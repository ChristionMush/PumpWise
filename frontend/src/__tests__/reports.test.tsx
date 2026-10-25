import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

const me = { "GET /api/auth/me/": { body: { user: ownerUser, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" } } }, "GET /api/reports/days/": { body: { days: [] } } };
const sales = { totals: { litres: "100", amount_by_currency: { USD: "150.00" } }, averages: { avg_daily_litres: "100", by_weekday: [] }, days: [] };
const dayReport = {
  station: "s1", station_name: "Chigwende", day: "2026-10-21", currency: "USD", closed: true, closed_at: "2026-10-21T19:05:00Z", closed_by: "Justice", so_far: false,
  totals: { litres: "105.000", amount: "157.50", handed_in: "115.00", grand_total: "157.50", received: "155.00", intercompany: "0.00" },
  handed_in: [{ payment_type: "cash", label: "Cash", amount: "155.00" }, { payment_type: "card", label: "Swipe", amount: "0.00" }, { payment_type: "mobile_money", label: "EcoCash", amount: "0.00" }, { payment_type: "credit", label: "Inter Company Credit", amount: "0.00" }],
  balance: [{ key: "evening", label: "Sales between 6pm & 9pm", amount: "40.00" }, { key: "short", label: "Short (+) / over (−)", amount: "2.50" }],
  cash_losses: { total: "2.50", attendants: "2.50", by_attendant: [{ attendant: "Tendai", amount: "2.50" }], milking: "0.00", milking_count: 0 },
  dips: [], fuels: [{ fuel: "Petrol", fuel_code: "PETROL", litres: "105.000", amount: "157.50" }], pumps: [], tanks: [], people: [],
  shifts: [{ shift_id: "sh1", started_at: "2026-10-20T16:00:00Z", ended_at: "2026-10-21T05:00:00Z", status: "closed", attendants: ["Tendai"], expected: "157.50", handed_in: "155.00",
    cash_difference: "-2.50", cash_status: "short", per_attendant_expected: true, by_attendant: [], by_pump: [{ pump_id: "p1", pump: "Pump 1", fuel_code: "PETROL", attendant: "u1", attendant_name: "Tendai", litres: "105.000", amount: "157.50" }] },
    { shift_id: "sh2", started_at: "2026-10-21T11:00:00Z", ended_at: "2026-10-21T16:30:00Z", status: "open", attendants: ["Rudo"], expected: "100.00", handed_in: "99.00",
      cash_difference: "-1.00", waiting: [{ name: "Ashok", expected: "200.00" }], people: [] }],
};
const person = { user: "u1", name: "Tendai", shifts: 2, shared_shifts: 0, flagged_shifts: 1, litres: "80.000", sales: "120.00", handed_in: "115.00", expected: "120.00", cash_difference: "-5.00",
  pumps: [{ pump: "Pump 1", fuel_code: "PETROL", litres: "80.000", amount: "120.00" }] };

describe("reports", () => {
  it("opens on the trading days, each with its grand total and its shifts as the drill-down", async () => {
    signedIn();
    mockApi({ ...me, "GET /api/reports/days/": { body: { days: [dayReport] } } });
    renderApp("/reports");
    const day = await screen.findByRole("region", { name: "Daily report Chigwende" });
    expect(day).toHaveTextContent(/Date: Wednesday,? (21 October|October 21),? 2026/);
    // the Grand Total is the fuel sold: the splits, the evening sales not handed in yet (6–9 pm), and 2.50 short
    expect(day).toHaveTextContent(/Grand Total\s*USD 157\D50/);
    expect(within(day).getByRole("table", { name: "Grand total" })).toHaveTextContent(/Sales between 6pm & 9pm\s*split into cash, swipe… when the night shift hands in\s*USD 40\D00.*Short \(\+\) \/ over \(−\)\s*attendants' cash differences\s*\+USD 2\D50/);
    const shifts = within(day).getByRole("table", { name: "Shifts" });
    expect(shifts).toHaveTextContent("Shifts (2)"); // a heading row, no dropdown
    expect(within(shifts).getByRole("row", { name: "Shift Tendai" })).toHaveTextContent(/157\D50 \/ 155\D00\s*-2\D50/);
    // an open shift: only those who handed in count, the rest are listed as still to hand in
    // a long-running shift (assigned pumps): each attendant's own turn, like any other shift; who still owes is listed
    expect(within(shifts).getByRole("row", { name: "Shift Rudo" })).toHaveTextContent(/100\D00 \/ 99\D00\s*-1\D00/);
    expect(shifts).toHaveTextContent(/Meters closed, cash not in yet: Ashok\s*owes USD 200\D00/);
  });


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
    expect(await screen.findByText("No days in this period.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sales" }));
    expect(await screen.findByText("Average 100 L per day")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Shifts: cash check" }));
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
    mockApi({ "GET /api/auth/me/": { body: { user: manager, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" } } }, "GET /api/reports/days/": { body: { days: [] } } });
    renderApp("/reports");
    expect(await screen.findByText("No days in this period.")).toBeInTheDocument();
    expect(screen.queryByText(/Accounting pack/)).not.toBeInTheDocument();
  });
});
