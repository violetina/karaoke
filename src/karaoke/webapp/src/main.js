import { createApp } from 'vue'
import App from './App.vue'
import { installAppShortcuts } from './appShortcuts.js'

installAppShortcuts()

createApp(App).mount('#app')
