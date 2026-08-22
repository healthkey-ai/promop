import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Self-contained "widget" build of the PatientInfo form — see
// src/federation/widget.patient-info.tsx for WHY this exists alongside the
// Module-Federation remote (vite.remote.config.ts). The remote SHARES React as a
// singleton with its host; this build BUNDLES its own React 19 + QueryClient + axios
// so a host on a different React version (CB's React 18 `ui/`) mounts it in isolation.
//
// Output: dist/widget/promop-patient-info.js — one ES module exporting `mount`/`unmount`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // A lib build does not auto-replace process.env.NODE_ENV; define it so React's checks
  // don't reach the browser as a bare `process` ("process is not defined").
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}",
  },
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  cacheDir: "node_modules/.vite-widget",
  build: {
    outDir: "dist/widget",
    target: "esnext",
    lib: {
      entry: path.resolve(__dirname, "src/federation/widget.patient-info.tsx"),
      formats: ["es"],
      fileName: () => "promop-patient-info.js",
    },
  },
});
