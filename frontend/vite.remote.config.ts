import dns from "node:dns";
import path from "node:path";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { federation } from "@module-federation/vite";

dns.setDefaultResultOrder("ipv4first");

function devHarnessRedirect(): Plugin {
  return {
    name: "dev-harness-redirect",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        if (req.url === "/" || req.url === "/index.html") {
          req.url = "/dev-harness.html";
        }
        next();
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || "http://localhost:9200";

  return {
    plugins: [
      devHarnessRedirect(),
      react(),
      tailwindcss(),
      federation({
        name: "labs_results_remote",
        filename: "remoteEntry.js",
        exposes: {
          "./LabResults": "./src/federation/LabResults.tsx",
          "./PatientInfo": "./src/federation/PatientInfo.tsx",
          // Framework-agnostic mounts for non-React hosts (HealthTree ONE).
          "./LabResultsBridge": "./src/federation/LabResultsBridge.tsx",
          "./PatientInfoBridge": "./src/federation/PatientInfoBridge.tsx",
          // React editors for hosts that share this React tree (ht-phr): the
          // host hands over its own authenticated client, not a token, so the
          // Firebase Bearer interceptor and the connection-scoped identity are
          // the same ones the rest of the host uses.
          "./TherapyLinesEditor": "./src/federation/TherapyLinesEditor.tsx",
          "./GenomicsEditor": "./src/federation/GenomicsEditor.tsx",
          "./PatientFieldEditorSlot": "./src/federation/PatientFieldEditorSlot.tsx",
          "./usePromopFieldBindings": "./src/federation/usePromopFieldBindings.ts",
          // One module for all of the above: a host cannot tell a failed
          // remote's `{ default: () => null }` stub from a real editor one
          // expose at a time. `PROMOP_EDITORS_API` is the marker.
          "./editors": "./src/federation/editors.ts",
          "./types": "./src/federation/types.ts",
        },
        shared: {
          react: { singleton: true, strictVersion: false },
          "react-dom": { singleton: true, strictVersion: false },
          "react/jsx-runtime": { singleton: true, strictVersion: false },
          "react/jsx-dev-runtime": { singleton: true, strictVersion: false },
          "@tanstack/react-query": { singleton: true, strictVersion: false },
          axios: { singleton: true, strictVersion: false },
          recharts: { singleton: true, strictVersion: false },
          "@radix-ui/react-dialog": { singleton: true, strictVersion: false },
          "@radix-ui/react-select": { singleton: true, strictVersion: false },
        },
        dts: false,
      }),
    ],
    resolve: {
      alias: {
        "@": path.resolve("src"),
      },
    },
    cacheDir: "node_modules/.vite-remote",
    build: {
      outDir: "dist/remote",
      target: "esnext",
    },
    server: {
      port: 3001,
      strictPort: true,
      proxy: {
        "/api": {
          target: apiProxyTarget,
          changeOrigin: true,
        },
        "/o": {
          target: apiProxyTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
