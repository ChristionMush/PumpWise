import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

const me = { "GET /api/auth/me/": { body: { user: ownerUser, tenant: { id: "t1", name: "Pesnat", slug: "p", status: "active" } } } };
const station = { id: "s1", name: "Mverechena", today: { litres: "0", amount_by_currency: {} }, tanks: [], open_shifts: [], unexplained_loss_7d: "0" };
const DASH = { totals: { today_litres: "0", today_amount_by_currency: {}, open_alerts: 0, unexplained_loss_7d: "0", flagged_cash_shifts_7d: 0, open_shifts: 0 }, next_refill: null, alerts: [] };

describe("a business that closes the day at the shift change", () => {
  it("the dashboard says the day is not closed and what holds it up", async () => {
    signedIn();
    const held = { day: "2026-11-11", change: "18:00", people: ["Abel"], tanks: ["Diesel Tank 4"] };
    mockApi({ ...me, "GET /api/dashboard/": { body: { ...DASH, stations: [{ ...station, day_not_closed: held }] } } });
    renderApp("/");
    expect(await screen.findByRole("alert")).toHaveTextContent(/is not closed: Abel still on; not dipped: Diesel Tank 4\. Close it on the Dip screen\./);
  });

  it("the dashboard says nothing once the day is closed", async () => {
    signedIn();
    mockApi({ ...me, "GET /api/dashboard/": { body: { ...DASH, stations: [{ ...station, day_not_closed: null }] } } });
    renderApp("/");
    await screen.findByText("Mverechena");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("the report shows the money not handed in yet on its own line", async () => {
    signedIn();
    const report = {
      station: "s1", station_name: "Mverechena", day: "2026-11-11", currency: "USD", closed: true, closed_at: "2026-11-11T14:31:00Z", closed_by: "Tawanda", so_far: false,
      totals: { litres: "10", amount: "100.00", handed_in: "60.00", grand_total: "100.00", received: "60.00", intercompany: "0.00" },
      handed_in: [{ payment_type: "cash", label: "Cash", amount: "60.00" }], balance: [{ key: "unsent", label: "Not handed in yet", amount: "40.00" }],
      cash_losses: { total: "0.00", attendants: "0.00", by_attendant: [], milking: "0.00", milking_count: 0 },
      dips: [], fuels: [], pumps: [], tanks: [], people: [], shifts: [],
    };
    mockApi({ ...me, "GET /api/reports/days/": { body: { days: [report] } } });
    renderApp("/reports");
    const day = await screen.findByRole("region", { name: "Daily report Mverechena" });
    expect(within(day).getByRole("table", { name: "Grand total" })).toHaveTextContent(/Not handed in yet\s*split into cash, swipe… when they hand in\s*USD 40\D00/);
  });
});
