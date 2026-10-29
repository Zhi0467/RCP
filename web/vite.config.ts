import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // `desktop:dev` starts this server beside a backend that RCP_PORT may move.
      "/api": {
        target: `http://127.0.0.1:${loadEnv(mode, ".", "RCP_").RCP_PORT || "8421"}`,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    // A running source-mode window may still lazy-load a chunk from the previous
    // build. Keep content-hashed assets until the server is restarted; packaging
    // uses `build:clean` so stale chunks never ship.
    emptyOutDir: false,
  },
}));
