// Keep browser shortcuts inside the dashboard when it runs as a desktop app.
//
// `make desktop-install` launches the dashboard with `google-chrome --app=...`.
// In an app window Chrome does not reserve its tab/window shortcuts: the page
// gets keydown first, and Chrome only acts if the page doesn't call
// preventDefault(). (In a normal tab Ctrl+T/N/W are reserved and never reach
// the page, so this guard only applies in app mode.)
//
// Swallowed combos are re-emitted as a cancelable `karaoke:shortcut`
// CustomEvent on window ({ detail: { combo, event } }) so components can bind
// them, e.g. window.addEventListener('karaoke:shortcut', e => {
//   if (e.detail.combo === 'Ctrl+T') openSomething() })

// Browser-chrome actions that make no sense in the app window. Left alone on
// purpose: Ctrl+W/Ctrl+Q (close app), Ctrl+R/F5 (reload), Ctrl+F (find),
// Ctrl+±/0 (zoom), Ctrl+Shift+I/J/C (DevTools), clipboard/undo.
const BLOCKED = new Set([
  'Ctrl+T', // new tab
  'Ctrl+Shift+T', // reopen closed tab
  'Ctrl+N', // new window
  'Ctrl+Shift+N', // new incognito window
  'Ctrl+D', // bookmark page
  'Ctrl+Shift+D', // bookmark all tabs
  'Ctrl+Shift+B', // bookmarks bar
  'Ctrl+Shift+O', // bookmark manager
  'Ctrl+H', // history
  'Ctrl+J', // downloads
  'Ctrl+O', // open file
  'Ctrl+S', // save page
  'Ctrl+P', // print
  'Ctrl+U', // view source
  'Ctrl+Shift+Delete', // clear browsing data
])

export function isAppWindow() {
  return ['standalone', 'minimal-ui', 'window-controls-overlay', 'fullscreen'].some(
    (mode) => window.matchMedia(`(display-mode: ${mode})`).matches,
  )
}

export function comboFor(event) {
  if (!(event.ctrlKey || event.metaKey) || event.altKey) return null
  let key = event.key
  if (key.length === 1) key = key.toUpperCase()
  return `Ctrl+${event.shiftKey ? 'Shift+' : ''}${key}`
}

export function installAppShortcuts() {
  if (window.__karaokeAppShortcuts || !isAppWindow()) return false
  window.__karaokeAppShortcuts = true
  // Capture phase on window: runs before any component handler can
  // stopPropagation() and before Chrome's post-dispatch accelerator handling.
  window.addEventListener(
    'keydown',
    (event) => {
      const combo = comboFor(event)
      if (!combo || !BLOCKED.has(combo)) return
      event.preventDefault()
      window.dispatchEvent(
        new CustomEvent('karaoke:shortcut', { detail: { combo, event }, cancelable: true }),
      )
    },
    { capture: true },
  )
  return true
}
