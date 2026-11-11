import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { litresFromChart } from "../pages/Dip";
import { mockApi, renderApp, signedIn } from "./helpers";

const manager = { id: "m", role: "manager", full_name: "Tatenda", email: null, phone: null, must_change_password: false, tenant_id: "t" };
const chart: [string, string][] = [["0.0", "0.000"], ["400.0", "6000.000"], ["800.0", "13500.000"]];

describe("litres from the dip chart", () => {
  it("reads between the two rows either side, like the server", () => {
    expect(litresFromChart(chart, "400")).toBe("6000");
    expect(litresFromChart(chart, "200")).toBe("3000");
    expect(litresFromChart(chart, "600")).toBe("9750");
    expect(litresFromChart(chart, "123,5")).toBe("1852.5");
  });
  it("gives nothing off the chart, without a number or without a chart", () => {
    expect(litresFromChart(chart, "900")).toBe("");
    expect(litresFromChart(chart, "")).toBe("");
    expect(litresFromChart(chart, "abc")).toBe("");
    expect(litresFromChart([], "200")).toBe("");
  });
});

describe("the day close fills the litres from the mm", () => {
  it("types the mm and the litres follow from that tank's chart", async () => {
    signedIn();
    const tanks = [
      { id: "k1", name: "Diesel tank", fuel_code: "DIESEL", has_calibration: true, calibration: chart },
      { id: "k2", name: "Petrol tank", fuel_code: "PETROL", has_calibration: false, calibration: [] },
    ];
    mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } } },
      "GET /api/shifts/prefill/": { body: { stations: [{ id: "s1", name: "Msasa Road", pricing_currency: "USD", pumps: [], prices: [], open_shift_id: null, tanks }] } },
      "GET /api/day-close/": { body: [] },
    });
    renderApp("/dip");
    const form = within(await screen.findByRole("form", { name: "Close the day" }));
    const mm = await form.findAllByLabelText("Dip (mm)");
    fireEvent.change(mm[0], { target: { value: "600" } });
    expect(form.getByLabelText(/Litres \(from the dip chart\)/)).toHaveValue("9750");
    fireEvent.change(mm[0], { target: { value: "900" } });
    expect(form.getByLabelText(/Litres \(from the dip chart\)/)).toHaveValue("");
    expect(form.getByText(/this height is off the chart/)).toBeInTheDocument();
    // a tank without a chart: the litres are typed by hand, nothing is filled
    fireEvent.change(mm[1], { target: { value: "600" } });
    expect(form.getByLabelText(/^Litres$/)).toHaveValue("");
  });
});
