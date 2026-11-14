/** @vitest-environment jsdom */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import Field from "./Field";

describe("Field accessible name", () => {
  it("falls back to the visible label when the override is blank", () => {
    render(
      <Field
        label="Height"
        name="height"
        type="number"
        value={170}
        ariaLabel="   "
        onChange={vi.fn()}
      />,
    );

    // A blank caller label must not erase the visible one from the a11y tree.
    expect(screen.getByRole("spinbutton", { name: "Height" })).toBeInTheDocument();
  });
});
