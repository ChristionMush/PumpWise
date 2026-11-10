import "fake-indexeddb/auto";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { resetOffline } from "../offline";
import { mockApi, renderApp, signedIn } from "./helpers";

const tenant = { id: "t", name: "Pesnat", slug: "p", status: "active", expenses_enabled: true };
const attendant = { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" };
const station = { id: "st1", name: "Main", pricing_currency: "USD", open_shift_id: null, pumps: [], prices: [] };
const applyAll = (body: any) => ({ body: { results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "applied" })) } });

beforeEach(async () => { await resetOffline(); localStorage.clear(); localStorage.setItem("fs_tutorial_done", "1"); signedIn(); });

describe("expenses", () => {
  it("lets an attendant record an expense, saved on the phone and sent when there is signal", async () => {
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: attendant, tenant } }, "GET /api/shifts/prefill/": { body: { stations: [station] } },
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
