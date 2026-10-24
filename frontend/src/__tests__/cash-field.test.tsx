import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { CashField } from "../components";

function Box({ type }: { type: string }) {
  const [v, setV] = useState("");
  return <CashField type={type} label={type === "cash" ? "Cash" : "Card / swipe"} value={v} onChange={setV} />;
}

describe("cash box", () => {
  it("says cash is counted in whole dollars, and warns when cents are typed", async () => {
    render(<Box type="cash" />);
    const box = screen.getByLabelText("Cash");
    expect(box).toHaveAccessibleDescription("Cash is counted in whole dollars");
    expect(box).toHaveAttribute("inputmode", "numeric");
    await userEvent.setup().type(box, "726.85");
    expect(box).toHaveValue("726.85"); // kept as typed, never turned into 72685
    expect(box).toHaveAccessibleDescription("Cash is counted in whole dollars: remove the cents");
  });

  it("lets swipe, EcoCash and intercompany credit have cents", async () => {
    render(<Box type="card" />);
    const box = screen.getByLabelText("Card / swipe");
    await userEvent.setup().type(box, "10,50");
    expect(box).toHaveValue("10.50");
    expect(box).not.toHaveAccessibleDescription();
  });
});
