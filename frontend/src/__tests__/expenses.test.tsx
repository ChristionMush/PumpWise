import "fake-indexeddb/auto";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { kvSet, resetOffline } from "../offline";
import { mockApi, renderApp, signedIn } from "./helpers";

const tenant = { id: "t", name: "Pesnat", slug: "p", status: "active", expenses_enabled: true };
const attendant = { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" };
const station = { id: "st1", name: "Main", pricing_currency: "USD", open_shift_id: null, pumps: [], prices: [] };
const applyAll = (body: any) => ({ body: { results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "applied" })) } });

beforeEach(async () => { await resetOffline(); localStorage.clear(); localStorage.setItem("fs_tutorial_done", "1"); signedIn(); });

describe("expenses", () => {
  it("lets an attendant add expenses under their cash at the end of their shift: a name and a cost each", async () => {
    await kvSet("activeShift", { id: "s1", station_id: "st1", started_at: "2026-11-10T05:00:00Z", starts: { n1: 1000 } });
    const onPump = { ...station, prices: [{ fuel_type: "p", currency: "USD", price: "1.50", from: "2026-01-01T00:00:00Z", to: null }],
      pumps: [{ id: "pu1", name: "Pump 1", nozzles: [{ id: "n1", number: 1, label: "Petrol", meter_max: "99999999.99", last_reading: "1000.00", fuel_type: "p", fuel_code: "PETROL" }] }] };
    const { calls } = mockApi({ "GET /api/auth/me/": { body: { user: attendant, tenant } }, "GET /api/shifts/prefill/": { body: { stations: [onPump] } }, "POST /api/sync/": applyAll });
    renderApp("/");
    const user = userEvent.setup();
    expect(screen.queryByRole("link", { name: "Expenses" })).not.toBeInTheDocument(); // attendants add theirs here, at the end
    await user.type(await screen.findByPlaceholderText("Meter number now"), "1100");
    await user.type(screen.getByLabelText("Cash"), "150");
    await user.click(screen.getByRole("button", { name: "+ Add expense" }));
    await user.type(screen.getByLabelText("What it was"), "Lunch");
    await user.click(screen.getByRole("button", { name: "End shift" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Each expense needs what it was and what it cost");
    await user.type(screen.getByLabelText("Cost"), "5");
    // worked out before handing in: 100 L x 1.50 = 150 expected, less the 5 for lunch = 145 to hand in; 150 cash is 5 over
    expect(screen.getByText("Hand in").parentElement).toHaveTextContent("145.00");
    expect(screen.getByText("Difference").parentElement).toHaveTextContent("5.00");
    await user.clear(screen.getByLabelText("Cash"));
    await user.type(screen.getByLabelText("Cash"), "145");
    expect(screen.getByText("Difference").parentElement).toHaveTextContent("0.00");
    await user.click(screen.getByRole("button", { name: "End shift" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/sync/" && c.body.ops.some((o: any) => o.kind === "shift_end"))).toBe(true));
    const end = calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops).find((o: any) => o.kind === "shift_end");
    expect(end.payload.cash).toEqual([{ payment_type: "cash", currency: "USD", amount: "145" }]); // the takings less the expense
    expect(end.payload.expenses).toEqual([{ client_id: expect.any(String), name: "Lunch", amount: "5" }]);
  });

  it("lets a manager record an expense on the Expenses screen, saved on the phone first", async () => {
    const manager = { ...attendant, id: "m", role: "manager", full_name: "Justice" };
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant } }, "GET /api/shifts/prefill/": { body: { stations: [station] } },
      "GET /api/expenses/": { body: [] }, "POST /api/sync/": applyAll,
    });
    renderApp("/expenses");
    const user = userEvent.setup();
    expect(await screen.findByRole("link", { name: "Expenses" })).toBeInTheDocument();
    await user.selectOptions(await screen.findByLabelText("Paid for"), "other");
    await user.type(screen.getByLabelText("Amount (USD)"), "7,50");
    await user.click(screen.getByRole("button", { name: "Save expense" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Say what the money was paid for");
    await user.type(screen.getByLabelText("What was it for"), "Airtime for the station phone");
    await user.click(screen.getByRole("button", { name: "Save expense" }));
    expect(await screen.findByText(/does not change the cash you hand in/, { selector: "[role=status]" })).toBeInTheDocument();
    await waitFor(() => expect(calls.some((c) => c.path === "/api/sync/")).toBe(true));
    const sent = calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops)[0];
    expect(sent.kind).toBe("expense");
    expect(sent.payload).toMatchObject({ station: "st1", category: "other", amount: "7.50", note: "Airtime for the station phone" });
  });

  it("shows the owner every expense with who paid it, and a total", async () => {
    const owner = { ...attendant, id: "o", role: "owner", full_name: "Rudo" };
    const row = (id: string, by: string, label: string, amount: string) => ({ id, station: "st1", station_name: "Main", category: "lunch", category_label: label,
      amount, currency: "USD", note: "", spent_at: "2026-11-10T10:00:00Z", recorded_by_name: by });
    mockApi({
      "GET /api/auth/me/": { body: { user: owner, tenant } }, "GET /api/shifts/prefill/": { body: { stations: [station] } },
      "GET /api/expenses/": { body: [row("e1", "Tendai", "Lunch", "5.00"), row("e2", "Rudo", "Generator fuel", "20.00")] },
    });
    renderApp("/expenses");
    const table = await screen.findByRole("table", { name: "Expenses" });
    expect(table).toHaveTextContent("Tendai");
    expect(table).toHaveTextContent("Generator fuel");
    expect(screen.getByText("Total: USD 25.00")).toBeInTheDocument();
  });

  it("is not offered to a business without expenses", async () => {
    mockApi({ "GET /api/auth/me/": { body: { user: attendant, tenant: { ...tenant, expenses_enabled: false } } }, "GET /api/shifts/prefill/": { body: { stations: [station] } } });
    renderApp("/");
    await screen.findByRole("link", { name: "Shift" });
    expect(screen.queryByRole("link", { name: "Expenses" })).not.toBeInTheDocument();
  });
});
