import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import axios from "axios";
import { LabResults } from "./LabResults";

const apiClient = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
  withCredentials: true,
});

export default function DevHarness() {
  return (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: "2rem" }}>
      <h1 style={{ marginBottom: "1rem", fontSize: "1.5rem", fontWeight: 700 }}>
        Labs Results Remote – Dev Harness
      </h1>
      <LabResults apiClient={apiClient} />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <DevHarness />
  </StrictMode>,
);
