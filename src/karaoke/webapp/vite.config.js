import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: '../webapp_dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/mood/embedded': { target: 'http://127.0.0.1:8900', changeOrigin: true },
      '/api': {
        target: 'http://127.0.0.1:8900',
        changeOrigin: true,
      },
    },
  },
})
