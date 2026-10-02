import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    target: "es2022",
    sourcemap: false,
    rollupOptions: {
      input: {
        main: resolve(process.cwd(), "index.html"),
        octave: resolve(process.cwd(), "octave/index.html"),
      },
      output: {
        // Keep the optional music workstation's synthesizer dependencies in
        // shared cacheable chunks instead of one oversized octave entry.
        manualChunks(id) {
          if (id.includes("/spessasynth_core@") || id.includes("/spessasynth_core/")) return "synth-core";
          if (id.includes("/spessasynth_lib@") || id.includes("/spessasynth_lib/")) return "synth-engine";
          return undefined;
        },
      },
    },
  },
});
