import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { parseChart } from "../pages/Calibration";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

describe("reading a calibration chart", () => {
  it("reads commas, semicolons, tabs or spaces, skips a header and sorts by dip height", () => {
    const { rows, errors } = parseChart("Dip (mm), Litres\n800; 13500\n0\t0\n400 6000\n");
    expect(errors).toEqual([]);
    expect(rows).toEqual([{ dip_mm: 0, litres: 0 }, { dip_mm: 400, litres: 6000 }, { dip_mm: 800, litres: 13500 }]);
  });

  it("explains bad rows by line number", () => {
    expect(parseChart("0, 0\n400\n800, 13500").errors[0]).toMatch(/^Line 2: expected two numbers/);
    expect(parseChart("0, 0\n400, 6000\n400, 7000").errors).toContain("Two rows have the same dip height.");
    expect(parseChart("0, 100\n400, 50").errors).toContain("Litres must go up as the dip height goes up.");
  });
});

describe("uploading a chart in Setup", () => {
  it("shows which tanks miss a chart and saves a pasted chart after a preview", async () => {
    signedIn();
    const tank = { id: "t1", station: "s1", name: "Diesel tank", fuel_code: "DIESEL", capacity_litres: "20000", has_calibration: false };
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: ownerUser, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" } } },
      "GET /api/stations/": { body: { results: [{ id: "s1", name: "Msasa Road" }] } },
      "GET /api/fuel-types/": { body: { results: [] } },
      "GET /api/tanks/": { body: { results: [tank] } },
      "GET /api/pumps/": { body: { results: [] } },
      "GET /api/onboarding/checklist/": { body: { items: [], done: 0, total: 1, percent: 0, ready: false } },
      "PUT /api/tanks/t1/calibration/": { body: { points: 3 } },
    });
    renderApp("/setup");
    const user = userEvent.setup();
    expect(await screen.findByRole("heading", { name: "Tank calibration charts (dip charts)" })).toBeInTheDocument();
    expect(screen.getByText("Missing")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Upload chart" }));
    await user.type(screen.getByLabelText("Or type or paste the rows"), "0, 0{enter}400, 6000{enter}800, 13500");
    expect(screen.getByText(/3 rows, 0 to 800 mm/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save chart" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ points: [{ dip_mm: 0, litres: 0 }, { dip_mm: 400, litres: 6000 }, { dip_mm: 800, litres: 13500 }] });
    expect(await screen.findByText(/Chart saved for Diesel tank/)).toBeInTheDocument();
  });
});
