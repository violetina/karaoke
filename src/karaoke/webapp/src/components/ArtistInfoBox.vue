<template>
  <div class="artist-infobox-card">
    <div class="infobox-header">
      <div class="header-tag">
        <span class="dot"></span>
        <span>ARTIST KNOWLEDGE BASE</span>
      </div>
      <div class="cache-badge" v-if="artist.cached_at || artist.wikidata">
        <span>⚡ OpenSearch Cached</span>
      </div>
    </div>

    <div v-if="loading" class="loading-state">
      <span class="spinner">⏳</span> Fetching artist &amp; Wikidata relations...
    </div>

    <div v-else-if="artist.name" class="infobox-content">
      <!-- Portrait & Core Identity -->
      <div class="profile-row">
        <div class="portrait" v-if="artist.image_url">
          <img :src="artist.image_url" :alt="artist.name" />
        </div>
        <div class="portrait placeholder" v-else>
          <span>👤</span>
        </div>

        <div class="identity">
          <div class="artist-name">{{ artist.name }}</div>
          <div class="disambiguation" v-if="artist.disambiguation">
            {{ artist.disambiguation }}
          </div>
          <div class="bio" v-if="artist.description">
            {{ artist.description }}
          </div>

          <div class="identifiers-row">
            <button
              class="play-artist-btn"
              @click="$emit('play-track', artist.name)"
              title="Play artist in kiosk player"
            >
              ▶ Play Artist
            </button>
            <a
              v-if="artist.wikidata"
              :href="`https://www.wikidata.org/wiki/${artist.wikidata}`"
              target="_blank"
              class="id-badge wikidata"
              title="Wikidata Persistent Identifier"
            >
              <span class="id-icon">🌐</span> {{ artist.wikidata }}
            </a>
            <span v-if="artist.mbid" class="id-badge mbid" title="MusicBrainz ID">
              MBID: {{ artist.mbid.slice(0, 8) }}…
            </span>
          </div>
        </div>
      </div>

      <!-- Quick Facts Grid -->
      <div class="facts-grid">
        <div class="fact-item" v-if="artist.country || (artist.countries && artist.countries.length)">
          <span class="fact-label">Origin:</span>
          <span class="fact-value">{{ artist.country || artist.countries?.join(', ') }}</span>
        </div>
        <div class="fact-item" v-if="artist.began_year">
          <span class="fact-label">Active:</span>
          <span class="fact-value">
            {{ artist.began_year }} – {{ artist.ended_year || 'Present' }}
          </span>
        </div>
        <div class="fact-item" v-if="artist.kind">
          <span class="fact-label">Type:</span>
          <span class="fact-value">{{ artist.kind }}</span>
        </div>
        <div class="fact-item" v-if="artist.track_count">
          <span class="fact-label">Library:</span>
          <span class="fact-value">{{ artist.track_count }} songs</span>
        </div>
      </div>

      <!-- Genres List -->
      <div class="section-block" v-if="artist.genres && artist.genres.length > 0">
        <div class="section-title">GENRES &amp; STYLES</div>
        <div class="tags-wrap">
          <span
            v-for="(g, i) in artist.genres.slice(0, 8)"
            :key="i"
            class="genre-tag"
          >
            {{ g }}
          </span>
        </div>
      </div>

      <!-- Band Members (for groups) -->
      <div class="section-block" v-if="artist.member_names && artist.member_names.length > 0">
        <div class="section-title">BAND MEMBERS</div>
        <div class="tags-wrap">
          <button
            v-for="(m, i) in artist.member_names"
            :key="i"
            class="relation-pill member-pill"
            @click="$emit('select-artist', artist.member_entities?.find(e => e.name === m) || m)"
          >
            👤 {{ m }}
          </button>
        </div>
      </div>

      <!-- Member Of / Side Projects (for individuals & bands) -->
      <div
        class="section-block"
        v-if="(artist.member_of_names && artist.member_of_names.length > 0) || (artist.side_projects && artist.side_projects.length > 0)"
      >
        <div class="section-title">BANDS &amp; SIDE PROJECTS</div>
        <div class="tags-wrap">
          <button
            v-for="(b, i) in allBands"
            :key="i"
            class="relation-pill band-pill"
            @click="$emit('select-artist', bandIdentity(b))"
          >
            🎸 {{ b }}
          </button>
        </div>
      </div>

      <!-- Influences -->
      <div class="section-block" v-if="artist.influenced_by_names && artist.influenced_by_names.length > 0">
        <div class="section-title">INFLUENCED BY</div>
        <div class="tags-wrap">
          <button
            v-for="(inf, i) in artist.influenced_by_names.slice(0, 6)"
            :key="i"
            class="relation-pill infl-pill"
            @click="$emit('select-artist', inf)"
          >
            ✨ {{ inf }}
          </button>
        </div>
      </div>

      <!-- External Links -->
      <div class="external-links-row" v-if="artist.links?.external_ids || artist.links?.wikipedia">
        <a
          v-if="artist.links.external_ids?.spotify"
          :href="`https://open.spotify.com/artist/${artist.links.external_ids.spotify}`"
          target="_blank"
          class="ext-link"
        >
          Spotify
        </a>
        <a
          v-if="artist.links.external_ids?.discogs"
          :href="`https://www.discogs.com/artist/${artist.links.external_ids.discogs}`"
          target="_blank"
          class="ext-link"
        >
          Discogs
        </a>
        <a
          v-if="artist.links.wikipedia?.en"
          :href="`https://en.wikipedia.org/wiki/${encodeURIComponent(artist.links.wikipedia.en)}`"
          target="_blank"
          class="ext-link"
        >
          Wikipedia
        </a>
      </div>
    </div>

    <div v-else class="empty-state">
      <span>Select an artist on the stage or in the relation graph to inspect their full Wikidata profile.</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  artist: {
    type: Object,
    default: () => ({}),
  },
  loading: {
    type: Boolean,
    default: false,
  },
})

defineEmits(['select-artist', 'play-track'])

const bandIdentity = (name) => {
  const entity = props.artist.member_of_entities?.find(e => e.name === name)
  if (entity) return entity
  const project = props.artist.side_projects?.find(e => e.name === name)
  return project?.qid ? { name, wikidata: project.qid } : name
}

const allBands = computed(() => {
  const list = [...(props.artist.member_of_names || [])]
  if (props.artist.side_projects) {
    for (const sp of props.artist.side_projects) {
      if (sp.name && !list.includes(sp.name)) {
        list.push(sp.name)
      }
    }
  }
  return list
})
</script>

<style scoped>
.artist-infobox-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 420px;
  min-height: 240px;
  flex-shrink: 0;
  overflow-y: auto;
}

.infobox-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}

.header-tag {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  font-weight: 800;
  color: var(--accent-cyan);
  letter-spacing: 0.08em;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-cyan);
  box-shadow: 0 0 6px var(--accent-cyan);
}

.cache-badge {
  font-size: 0.7rem;
  color: var(--accent-gold);
  font-family: 'JetBrains Mono', monospace;
}

.loading-state {
  padding: 40px 10px;
  text-align: center;
  color: var(--text-muted);
  font-size: 0.9rem;
}

.infobox-content {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.profile-row {
  display: flex;
  gap: 14px;
  align-items: flex-start;
}

.portrait {
  width: 90px;
  height: 90px;
  border-radius: 10px;
  overflow: hidden;
  flex-shrink: 0;
  border: 2px solid var(--border-bright);
  background: #181f30;
}

.portrait img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.portrait.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 2.2rem;
}

.identity {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  min-width: 0;
}

.artist-name {
  font-size: 1.35rem;
  font-weight: 800;
  color: #fff;
  line-height: 1.2;
}

.disambiguation {
  font-size: 0.8rem;
  color: var(--accent-pink);
  font-style: italic;
}

.bio {
  font-size: 0.8rem;
  color: var(--text-muted);
  line-height: 1.35;
  margin-top: 2px;
}

.identifiers-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  align-items: center;
}

.play-artist-btn {
  background: linear-gradient(135deg, var(--accent-cyan), #0077b6);
  border: none;
  border-radius: 4px;
  color: #07090e;
  font-weight: 800;
  font-size: 0.72rem;
  padding: 3px 10px;
  cursor: pointer;
  transition: transform 0.15s ease;
}

.play-artist-btn:hover {
  transform: scale(1.05);
}

.id-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.7rem;
  padding: 2px 7px;
  border-radius: 4px;
  text-decoration: none;
}

.id-badge.wikidata {
  background: rgba(0, 242, 254, 0.1);
  border: 1px solid rgba(0, 242, 254, 0.3);
  color: var(--accent-cyan);
}

.id-badge.wikidata:hover {
  background: rgba(0, 242, 254, 0.25);
  border-color: var(--accent-cyan);
}

.id-badge.mbid {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  color: var(--text-dim);
}

.facts-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  background: rgba(0, 0, 0, 0.25);
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid var(--border);
}

.fact-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.fact-label {
  font-size: 0.65rem;
  font-weight: 800;
  color: var(--text-dim);
  text-transform: uppercase;
}

.fact-value {
  font-size: 0.8rem;
  color: #e2e8f0;
  font-weight: 600;
}

.section-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.section-title {
  font-size: 0.7rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  color: var(--text-dim);
}

.tags-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.genre-tag {
  font-size: 0.75rem;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  color: #cbd5e1;
}

.relation-pill {
  font-size: 0.75rem;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 6px;
  border: 1px solid;
  cursor: pointer;
  background: transparent;
  transition: all 0.2s ease;
}

.member-pill {
  border-color: rgba(255, 190, 11, 0.3);
  color: var(--accent-gold);
}

.member-pill:hover {
  background: rgba(255, 190, 11, 0.15);
  border-color: var(--accent-gold);
}

.band-pill {
  border-color: rgba(255, 0, 127, 0.3);
  color: var(--accent-pink);
}

.band-pill:hover {
  background: rgba(255, 0, 127, 0.15);
  border-color: var(--accent-pink);
}

.infl-pill {
  border-color: rgba(72, 202, 228, 0.3);
  color: #48cae4;
}

.infl-pill:hover {
  background: rgba(72, 202, 228, 0.15);
  border-color: #48cae4;
}

.external-links-row {
  display: flex;
  gap: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}

.ext-link {
  font-size: 0.75rem;
  color: var(--text-muted);
  text-decoration: none;
}

.ext-link:hover {
  color: var(--accent-cyan);
}

.empty-state {
  text-align: center;
  color: var(--text-dim);
  font-size: 0.85rem;
  padding: 40px 10px;
  font-style: italic;
}
</style>
