import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `npm run build` writes the app to ../public, which Vercel serves (and Flask locally).
// `npm run dev` runs on :5173 and forwards /api to Flask on :5000.
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../public', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:5000' },
  },
});
