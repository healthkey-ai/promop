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
        "@": path.resolve(__dirname, "./src"),
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
