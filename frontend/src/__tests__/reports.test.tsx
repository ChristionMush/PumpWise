import { screen, within } from "@testing-library/react";
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

  it("lists each tank's checks newest first with their sums, and the running difference in the latest", async () => {
    signedIn();
    const day = (date: string, opening: string, sold: string, closing: string, loss: string, status: string) => ({ tank_id: "k1", tank: "Diesel Tank", fuel_code: "DIESEL", date, status,
      flagged: status !== "ok", loss, loss_pct: null, opening, closing, deliveries: "0", dispensed: sold, allowed: "3", window_start: `${date}T07:00:00Z`, window_end: `${date}T19:00:00Z` });
    mockApi({
      ...me,
      "GET /api/reports/daily-sales/": { body: sales },
      "GET /api/reconciliation/tanks/": { body: { rows: [day("2026-10-20", "5358.42", "446", "4843", "69.420", "loss"), day("2026-10-21", "4843", "707", "4214", "-78.000", "gain")],
        totals: [{ tank_id: "k1", tank: "Diesel Tank", fuel_code: "DIESEL", unexplained: "-8.580", sold: "1153.000", allowed: "5.765", pct: "-0.74", checks: 2, status: "gain" }] } },
    });
    renderApp("/reports");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Fuel check" }));
    const checks = within(await screen.findByRole("region", { name: "Diesel Tank fuel checks" })).getAllByText(/^2026-10-2/);
    expect(checks.map((c) => c.textContent)).toEqual(["2026-10-21 · latest", "2026-10-20"]); // newest first
    const total = screen.getByRole("region", { name: "Diesel Tank running difference" });
    expect(total).toHaveTextContent(/Running difference: -8[.,]58 L/);
    expect(total).toHaveTextContent(/= \+69[.,]42 \+ -78 L/); // the daily differences that add up to it
    expect(total.closest("section.card")).toHaveClass("good");
    expect(total.closest("section.card")).toHaveTextContent(/4[ ,.  ]?843 \+ 0 − 707 − 4[ ,.  ]?214 = -78 L/); // its own sum
  });

  it("shows the accounting pack to the owner only", async () => {
    signedIn();
    mockApi({ ...me, "GET /api/reports/daily-sales/": { body: sales } });
    renderApp("/reports");
    expect(await screen.findByText(/Accounting pack \(Excel\)/)).toBeInTheDocument();
  });

  it("does not show the accounting pack to a manager", async () => {
    signedIn();
    const manager = { ...ownerUser, id: "m", role: "manager", full_name: "Justice" };
    mockApi({ "GET /api/auth/me/": { body: { user: manager, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" } } }, "GET /api/reports/daily-sales/": { body: sales } });
    renderApp("/reports");
    expect(await screen.findByText("Average 100 L per day")).toBeInTheDocument();
    expect(screen.queryByText(/Accounting pack/)).not.toBeInTheDocument();
  });
});
