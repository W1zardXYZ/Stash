import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = `http://localhost:${process.env.STASH_PORT || 8420}`;

export default defineConfig(({ command }) => ({
  plugins: [react()],
  // the dev server only proxies /api: Blender/Unreal/scripts must talk to the API server itself
  define: { __DEV_API_ORIGIN__: JSON.stringify(command === "serve" ? api : null) },
  server: {
    port: 5173,
    proxy: { "/api": api },
  },
  build: { chunkSizeWarningLimit: 1500 },
}));
