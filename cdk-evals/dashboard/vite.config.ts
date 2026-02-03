import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Load env from parent cdk-evals directory
  const env = loadEnv(mode, path.resolve(__dirname, '..'), '')

  return {
    plugins: [react(), tailwindcss()],
    server: {
      proxy: {
        '/runs_index.json': {
          target: env.CLOUDFRONT_URL,
          changeOrigin: true,
          secure: true,
        },
        '/runs': {
          target: env.CLOUDFRONT_URL,
          changeOrigin: true,
          secure: true,
        },
      },
    },
  }
})
