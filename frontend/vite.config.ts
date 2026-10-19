import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": `http://localhost:${process.env.STASH_PORT || 8420}` },
  },
  build: { chunkSizeWarningLimit: 1500 },
});
