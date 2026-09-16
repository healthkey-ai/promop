import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import ConceptInputDetails from "./ConceptInputDetails";

describe("destination concept input details", () => {
  it("shows quantitative measurements with their suggested unit", () => {
    render(<ConceptInputDetails domain_id="Measurement" measurement_type="quantitative" suggested_unit="mg/dL" />);
    expect(screen.getByText("Quantitative · Unit: mg/dL")).toBeInTheDocument();
  });
  it("shows quantitative measurements without inventing units", () => {
    render(<ConceptInputDetails domain_id="Measurement" measurement_type="quantitative" />);
    expect(screen.getByText("Quantitative")).toBeInTheDocument();
  });
  it("shows qualitative measurements without numeric unit cues", () => {
    render(<ConceptInputDetails domain_id="Measurement" measurement_type="qualitative" suggested_unit="mg/dL" />);
    expect(screen.getByText("Qualitative")).toBeInTheDocument();
    expect(screen.queryByText(/Unit:/)).not.toBeInTheDocument();
  });
  it("explicitly identifies measurements with unknown input types", () => {
    render(<ConceptInputDetails domain_id="Measurement" />);
    expect(screen.getByText("Measurement · Input type unavailable")).toBeInTheDocument();
  });
  it("labels other domains without implying a measurement type", () => {
    render(<ConceptInputDetails domain_id="Condition" measurement_type="quantitative" suggested_unit="mg/dL" />);
    expect(screen.getByText("Condition")).toBeInTheDocument();
    expect(screen.queryByText(/Quantitative|Unit:/)).not.toBeInTheDocument();
  });
});
