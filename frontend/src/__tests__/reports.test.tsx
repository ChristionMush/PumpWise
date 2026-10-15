import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

const me = { "GET /api/auth/me/": { body: { user: ownerUser, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" } } } };
const sales = { totals: { litres: "100", amount_by_currency: { USD: "150.00" } }, averages: { avg_daily_litres: "100", by_weekday: [] }, days: [] };
const person = { user: "u1", name: "Tendai", shifts: 2, shared_shifts: 0, flagged_shifts: 1, litres: "80.000", sales: "120.00", handed_in: "115.00", expected: "120.00", cash_difference: "-5.00",
  pumps: [{ pump: "Pump 1", fuel_code: "PETROL", litres: "80.000", amount: "120.00" }] };

describe("reports", () => {
  it("switches tabs without drawing one tab's data as another (no blank screen)", async () => {
    signedIn();
    mockApi({
      ...me,
      "GET /api/reports/daily-sales/": { body: sales },
      "GET /api/reconciliation/cash/": { body: { rows: [] } },
      "GET /api/reconciliation/tanks/": { body: { rows: [] } },
      "GET /api/reports/attendants/": { body: { rows: [person] } },
    });
    renderApp("/reports");
    const user = userEvent.setup();
    expect(await screen.findByText("Average 100 L per day")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cash check" }));
    expect(await screen.findByText("No closed shifts in this period.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fuel check" }));
    expect(await screen.findByText(/No tank checks yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "By attendant" }));
    expect(await screen.findByText("Tendai")).toBeInTheDocument();
    expect(screen.getByText("-5.00")).toBeInTheDocument();
  });
});
