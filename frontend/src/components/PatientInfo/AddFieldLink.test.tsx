import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import AddFieldLink from "./AddFieldLink";
import { FieldMappingLinksProvider, addFieldHref, addFieldReturn, returnedTab } from "./fieldMappingLinks";

const renderLink = (on: boolean, props: { tab: string; disease?: string }) =>
  render(
    <MemoryRouter>
      <FieldMappingLinksProvider value={on}>
        <AddFieldLink {...props} returnPath="/patient/42" />
      </FieldMappingLinksProvider>
    </MemoryRouter>,
  );

describe("AddFieldLink", () => {
  it("opens Field Concept Mapping in create mode on its tab", () => {
    renderLink(true, { tab: "labs" });
    expect(screen.getByRole("link", { name: "Add Field" })).toHaveAttribute("href", "/field-mappings?new=1&tab=labs");
  });

  it("names the record's disease from the disease tab, and nothing about the patient", () => {
    renderLink(true, { tab: "disease", disease: "MM" });
    const href = screen.getByRole("link", { name: "Add Field" }).getAttribute("href");
    expect(href).toBe("/field-mappings?new=1&tab=disease&disease=MM");
    expect(href).not.toContain("42");
  });

  it("is not offered where field mapping links are off, or without a provider", () => {
    const { unmount } = renderLink(false, { tab: "labs" });
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    unmount();
    render(<MemoryRouter><AddFieldLink tab="labs" returnPath="/patient/42" /></MemoryRouter>);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("add field links", () => {
  it("builds the create link", () => {
    expect(addFieldHref("general")).toBe("/field-mappings?new=1&tab=general");
    expect(addFieldHref("disease", "CLL")).toBe("/field-mappings?new=1&tab=disease&disease=CLL");
  });

  it("follows a return only to a patient page", () => {
    expect(addFieldReturn({ addFieldReturn: { path: "/patient/42", tab: "labs" } }))
      .toEqual({ path: "/patient/42", tab: "labs" });
    for (const path of ["https://elsewhere.example/", "//elsewhere.example/patient/1", "/patient/1/../../admin", "/field-mappings"]) {
      expect(addFieldReturn({ addFieldReturn: { path, tab: "labs" } })).toBeNull();
    }
    expect(addFieldReturn(null)).toBeNull();
    expect(addFieldReturn({ addFieldReturn: { path: "/patient/42" } })).toBeNull();
  });

  it("reads the tab a patient page is returned to", () => {
    expect(returnedTab({ tab: "labs" })).toBe("labs");
    expect(returnedTab({ tab: 3 })).toBeNull();
    expect(returnedTab(null)).toBeNull();
  });
});
