import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'EVALFORGE_')

  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': env.EVALFORGE_API_TARGET || 'http://127.0.0.1:8000',
      },
    },
  }
})
