<template>
  <div class="ai-dj-dock" :class="{ expanded: isExpanded }">
    <!-- Dock Header / Toggle Bar -->
    <div class="dock-header" @click="toggleExpanded">
      <div class="left-branding">
        <div class="dj-avatar">
          <span class="eq-bar bar-1"></span>
          <span class="eq-bar bar-2"></span>
          <span class="eq-bar bar-3"></span>
        </div>
        <div class="dj-title">
          <span class="name">AI DJ &amp; MC</span>
          <span class="model-badge">Ollama qwen3 · MCP Integrated</span>
        </div>
      </div>

      <div class="header-summary" v-if="!isExpanded && latestMessage">
        <span class="latest-preview">{{ latestMessage.content }}</span>
      </div>

      <div class="right-controls">
        <span class="expand-indicator">{{ isExpanded ? '▼ Minimize' : '▲ Open AI DJ' }}</span>
      </div>
    </div>

    <!-- Expanded Chat Body -->
    <div class="dock-body" v-if="isExpanded">
      <div class="messages-container" ref="messagesRef">
        <div
          v-for="(msg, idx) in messages"
          :key="idx"
          class="chat-bubble"
          :class="msg.role"
        >
          <div class="bubble-sender">{{ msg.role === 'user' ? 'YOU' : 'AI DJ' }}</div>
          <div class="bubble-content">{{ msg.content }}</div>
          <button v-for="track in msg.queueSuggestions || []" :key="track" class="chip-btn"
            @click="$emit('queue-track', track)">Queue next: {{ track }}</button>
        </div>

        <div v-if="streamingText" class="chat-bubble assistant streaming">
          <div class="bubble-sender">AI DJ <span class="streaming-dot"></span></div>
          <div class="bubble-content">{{ streamingText }}</div>
        </div>
      </div>

      <!-- Quick Action Prompt Chips -->
      <div class="prompt-chips-row">
        <button
          v-for="(chip, i) in quickChips"
          :key="i"
          class="chip-btn"
          @click="sendPrompt(chip)"
          :disabled="isGenerating"
        >
          {{ chip }}
        </button>
      </div>

      <!-- Input Bar -->
      <div class="input-form">
        <input
          v-model="inputQuery"
          type="text"
          placeholder="Ask the AI DJ to suggest songs, explain bands, or hype the crowd..."
          @keyup.enter="handleSend"
          :disabled="isGenerating"
        />
        <button class="send-btn" @click="handleSend" :disabled="!inputQuery.trim() || isGenerating">
          {{ isGenerating ? 'DJ thinking...' : 'Send ⚡' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'

const props = defineProps({
  currentArtist: {
    type: String,
    default: '',
  },
  currentTitle: {
    type: String,
    default: '',
  },
})

const emit = defineEmits(['focus-artist', 'queue-track'])

const isExpanded = ref(true)
const isGenerating = ref(false)
const inputQuery = ref('')
const streamingText = ref('')
const queueSuggestions = ref([])
const messagesRef = ref(null)

const messages = ref([
  {
    role: 'assistant',
    content: "Hey party! I'm your AI DJ & MC. Ask me for harmonic next tracks, band backstories, or crowd hype!",
  },
])

const latestMessage = computed(() => {
  return messages.value[messages.value.length - 1]
})

const quickChips = computed(() => [
  `Suggest next song for ${props.currentArtist || 'now'}`,
  `What's the vibe of ${props.currentTitle || 'this song'}?`,
  `Tell me about ${props.currentArtist || 'this artist'} side projects`,
  'Find songs in harmonic key',
  'Queue an upbeat crowd pleaser!',
])

const toggleExpanded = () => {
  isExpanded.value = !isExpanded.value
}

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesRef.value) {
      messagesRef.value.scrollTop = messagesRef.value.scrollHeight
    }
  })
}

const sendPrompt = (text) => {
  inputQuery.value = text
  handleSend()
}

const handleSend = async () => {
  const query = inputQuery.value.trim()
  if (!query || isGenerating.value) return

  messages.value.push({ role: 'user', content: query })
  inputQuery.value = ''
  isGenerating.value = true
  streamingText.value = ''
  queueSuggestions.value = []
  scrollToBottom()

  try {
    const response = await fetch('/api/dj/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: query,
        current_artist: props.currentArtist,
        current_title: props.currentTitle,
      }),
    })

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { value, done } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() // keep incomplete chunk

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            if (data.token) {
              streamingText.value += data.token
              scrollToBottom()
            }
            if (data.action === 'focus_artist' && data.artist) {
              emit('focus-artist', data.artist)
            }
            if (['queue_suggestion', 'queue_track'].includes(data.action) && data.track) {
              // Even legacy server action frames only become a confirmation button.
              if (!queueSuggestions.value.includes(data.track)) queueSuggestions.value.push(data.track)
            }
          } catch (err) {
            // non-json line
          }
        }
      }
    }

    if (streamingText.value) {
      messages.value.push({ role: 'assistant', content: streamingText.value, queueSuggestions: [...queueSuggestions.value] })
      streamingText.value = ''
    }
  } catch (err) {
    messages.value.push({
      role: 'assistant',
      content: `DJ mic feedback: ${err.message}`,
    })
  } finally {
    isGenerating.value = false
    scrollToBottom()
  }
}
</script>

<style scoped>
.ai-dj-dock {
  background: var(--bg-surface);
  border-top: 1px solid var(--border-bright);
  box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.5);
  display: flex;
  flex-direction: column;
  transition: all 0.3s ease;
  z-index: 100;
}

.dock-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px;
  background: rgba(18, 24, 38, 0.95);
  cursor: pointer;
  user-select: none;
}

.left-branding {
  display: flex;
  align-items: center;
  gap: 12px;
}

.dj-avatar {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 18px;
}

.eq-bar {
  width: 3px;
  background: var(--accent-cyan);
  border-radius: 2px;
  animation: eq-bounce 1s infinite alternate ease-in-out;
}

.bar-1 { height: 8px; animation-delay: 0.1s; }
.bar-2 { height: 16px; animation-delay: 0.3s; background: var(--accent-pink); }
.bar-3 { height: 11px; animation-delay: 0.2s; background: var(--accent-gold); }

@keyframes eq-bounce {
  0% { transform: scaleY(0.4); }
  100% { transform: scaleY(1.2); }
}

.dj-title {
  display: flex;
  flex-direction: column;
}

.name {
  font-size: 0.95rem;
  font-weight: 800;
  color: #fff;
  letter-spacing: 0.05em;
}

.model-badge {
  font-size: 0.65rem;
  color: var(--text-muted);
  font-family: 'JetBrains Mono', monospace;
}

.header-summary {
  flex: 1;
  max-width: 50%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 0.8rem;
  color: var(--text-muted);
  padding: 0 16px;
}

.right-controls {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--accent-cyan);
}

.dock-body {
  display: flex;
  flex-direction: column;
  padding: 14px 20px 18px;
  gap: 12px;
  max-height: 320px;
}

.messages-container {
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow-y: auto;
  max-height: 180px;
  padding-right: 6px;
}

.chat-bubble {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 85%;
  padding: 8px 14px;
  border-radius: 10px;
  font-size: 0.88rem;
  line-height: 1.4;
}

.chat-bubble.assistant {
  align-self: flex-start;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  color: #f1f5f9;
}

.chat-bubble.user {
  align-self: flex-end;
  background: rgba(0, 242, 254, 0.15);
  border: 1px solid rgba(0, 242, 254, 0.4);
  color: #fff;
}

.bubble-sender {
  font-size: 0.65rem;
  font-weight: 800;
  color: var(--text-dim);
  letter-spacing: 0.05em;
}

.streaming-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-pink);
  animation: pulse 0.8s infinite;
  margin-left: 4px;
}

.prompt-chips-row {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 2px;
}

.chip-btn {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.75rem;
  padding: 4px 10px;
  border-radius: 20px;
  white-space: nowrap;
  cursor: pointer;
  transition: all 0.2s ease;
}

.chip-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
  border-color: var(--accent-cyan);
}

.input-form {
  display: flex;
  gap: 8px;
}

.input-form input {
  flex: 1;
  background: rgba(0, 0, 0, 0.4);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 14px;
  color: #fff;
  font-size: 0.88rem;
  outline: none;
  font-family: inherit;
}

.input-form input:focus {
  border-color: var(--accent-cyan);
  box-shadow: 0 0 10px rgba(0, 242, 254, 0.2);
}

.send-btn {
  background: linear-gradient(135deg, var(--accent-cyan), #0077b6);
  border: none;
  border-radius: 8px;
  color: #07090e;
  font-weight: 800;
  font-size: 0.85rem;
  padding: 0 18px;
  cursor: pointer;
  transition: opacity 0.2s ease;
}

.send-btn:hover:not(:disabled) {
  opacity: 0.9;
}

.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.ai-dj-dock.embedded {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: none;
  overflow: hidden;
  z-index: auto;
}
.embedded .dock-header { padding: 8px 10px; gap: 8px; }
.embedded .dock-body { padding: 10px; max-height: none; min-height: 250px; }
.embedded .messages-container { height: 150px; min-height: 150px; max-height: 220px; }
.embedded .chat-bubble { flex-shrink: 0; max-width: 100%; overflow-wrap: anywhere; }
.embedded .input-form input { min-width: 0; width: 0; }
.embedded .prompt-chips-row { flex-shrink: 0; }
.embedded .send-btn { padding: 0 10px; }
</style>
