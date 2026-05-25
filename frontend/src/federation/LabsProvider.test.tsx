import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import axios from "axios";
import { QueryClient } from "@tanstack/react-query";
import { LabsProvider } from "./LabsProvider";
import { useLabsContext } from "./LabsContext";

function ContextProbe() {
  const { apiBasePath } = useLabsContext();
  return <span data-testid="basepath">{apiBasePath}</span>;
}

describe("LabsProvider", () => {
  it("provides default apiBasePath", () => {
    const client = axios.create();
    render(
      <LabsProvider apiClient={client}>
        <ContextProbe />
      </LabsProvider>,
    );
    expect(screen.getByTestId("basepath").textContent).toBe("");
  });

  it("provides custom apiBasePath", () => {
    const client = axios.create();
    render(
      <LabsProvider apiClient={client} apiBasePath="/v2">
        <ContextProbe />
      </LabsProvider>,
    );
    expect(screen.getByTestId("basepath").textContent).toBe("/v2");
  });

  it("renders with hk-labs-root class", () => {
    const client = axios.create();
    const { container } = render(
      <LabsProvider apiClient={client}>
        <span>child</span>
      </LabsProvider>,
    );
    expect(container.querySelector(".hk-labs-root")).toBeTruthy();
  });

  it("applies custom className", () => {
    const client = axios.create();
    const { container } = render(
      <LabsProvider apiClient={client} className="my-class">
        <span>child</span>
      </LabsProvider>,
    );
    expect(container.querySelector(".my-class")).toBeTruthy();
  });

  it("sets CSS custom properties from theme", () => {
    const client = axios.create();
    const { container } = render(
      <LabsProvider apiClient={client} theme={{ colorPrimary: "200 80% 40%" }}>
        <span>child</span>
      </LabsProvider>,
    );
    const root = container.querySelector(".hk-labs-root") as HTMLElement;
    expect(root.style.getPropertyValue("--hk-labs-brand-700")).toBe("200 80% 40%");
  });

  it("uses external QueryClient when provided", () => {
    const client = axios.create();
    const qc = new QueryClient();
    render(
      <LabsProvider apiClient={client} queryClient={qc}>
        <span>child</span>
      </LabsProvider>,
    );
    expect(screen.getByText("child")).toBeInTheDocument();
  });
});
