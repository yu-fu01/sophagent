import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { resolve } from "path";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { "@": resolve(__dirname, "src") },
  },
  build: {
    outDir: resolve(__dirname, "../web/dist"),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/v1": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
});
