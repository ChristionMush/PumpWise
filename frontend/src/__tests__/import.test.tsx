import "fake-indexeddb/auto";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { resetOffline } from "../offline";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

const tenant = { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" };
const manager = { ...ownerUser, id: "m1", role: "manager" };
const DASH = { totals: { today_litres: "0", today_amount_by_currency: {}, open_alerts: 0, unexplained_loss_7d: "0", flagged_cash_shifts_7d: 0, open_shifts: 0 }, next_refill: null, stations: [], alerts: [] };
const SUMMARY = { shifts: 2, readings: 8, cash_rows: 3, from: "2026-03-01T04:00:00Z", to: "2026-03-02T16:00:00Z", stations: ["Main"], litres: "300.000",
  amount_by_currency: { USD: "450.00" }, flagged_shifts: 0, prices_added: [{ station: "Main", fuel: "Petrol", currency: "USD", price_per_litre: "1.50", from: "2026-03-01T04:00:00Z", to: "2026-04-01T00:00:00Z" }] };
const csv = () => new File(["station,date\n"], "meters.csv", { type: "text/csv" });

beforeEach(async () => { await resetOffline(); localStorage.clear(); localStorage.setItem("fs_tutorial_done", "1"); signedIn(); });

describe("importing old shifts", () => {
  it("checks the files first, then imports only when the owner confirms", async () => {
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: ownerUser, tenant } }, "GET /api/prices/announcements/": { body: { announcements: [] } },
      "POST /api/shifts/import/": (body) => ({ status: body.get("commit") === "false" ? 200 : 500, body: { ...SUMMARY, saved: false } }),
    });
    renderApp("/import");
    const user = userEvent.setup();
    expect(await screen.findByRole("link", { name: "Import" })).toBeInTheDocument();
    await user.upload(screen.getByLabelText("Meters file"), csv());
    await user.click(screen.getByRole("button", { name: "Check files" }));
    expect(await screen.findByRole("heading", { name: "Ready to import" })).toBeInTheDocument();
    expect(screen.getByText(/Petrol at Main: USD 1.50/)).toBeInTheDocument();
    expect(calls.filter((c) => c.path === "/api/shifts/import/")).toHaveLength(1);

    mockApi({ "GET /api/auth/me/": { body: { user: ownerUser, tenant } }, "POST /api/shifts/import/": { status: 201, body: { ...SUMMARY, saved: true } } });
    await user.click(screen.getByRole("button", { name: "Import 2 shifts" }));
    expect(await screen.findByRole("heading", { name: "Imported 2 shifts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Import \d/ })).not.toBeInTheDocument();
  });

  it("lists every row to fix and offers no import", async () => {
    mockApi({
      "GET /api/auth/me/": { body: { user: ownerUser, tenant } },
      "POST /api/shifts/import/": { status: 400, body: { errors: [{ file: "meters", row: 4, message: "No pump called 'Pump 9' at Main." }, { file: "cash", row: 2, message: "Amount 'x' is not a number." }] } },
    });
    renderApp("/import");
    const user = userEvent.setup();
    await user.upload(await screen.findByLabelText("Meters file"), csv());
    await user.click(screen.getByRole("button", { name: "Check files" }));
    const box = await screen.findByRole("alert");
    expect(box).toHaveTextContent("Meters row 4: No pump called 'Pump 9' at Main.");
    expect(box).toHaveTextContent("Cash row 2: Amount 'x' is not a number.");
    expect(screen.queryByRole("button", { name: /Import \d/ })).not.toBeInTheDocument();
  });

  it("is not offered to a manager", async () => {
    mockApi({ "GET /api/auth/me/": { body: { user: manager, tenant } }, "GET /api/dashboard/": { body: DASH }, "GET /api/prices/announcements/": { body: { announcements: [] } } });
    renderApp("/import");
    expect(await screen.findByRole("heading", { name: "Today" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("link", { name: "Import" })).not.toBeInTheDocument());
  });
});
