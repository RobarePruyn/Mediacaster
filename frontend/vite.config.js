import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    // Dev server port — matches the old CRA default
    port: 3000,
    // Proxy API requests to the FastAPI backend during development
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  build: {
    // Output to dist/ (Vite default) — nginx and FastAPI are configured to serve from here
    outDir: 'dist',
    sourcemap: false,
  },
});
