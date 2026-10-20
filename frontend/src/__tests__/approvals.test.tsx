import "fake-indexeddb/auto";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { resetOffline } from "../offline";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

const tenant = { id: "t", name: "Msasa", slug: "m", status: "active" };
const attendant = { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" };
const manager = { ...ownerUser, id: "m1", role: "manager", full_name: "Farai" };
const end = {
  shift_id: "s1", station: "Main", status: "pending", ended_at: "2026-10-20T16:00:00Z", note: "", person: "Tendai", currency: "USD",
  meters: [{ nozzle: "n1", pump: "Pump 1", label: "Petrol", fuel_code: "PETROL", start: "1000.00", end: "1100.00", litres: "100.000", amount: "150.00" }],
  cash: [{ payment_type: "cash", currency: "USD", amount: "120.00" }], handed_in: "120.00", expected: "150.00", cash_difference: "-30.00", undo: null,
};
const station = (my_stage: any) => ({ id: "st1", name: "Main", pricing_currency: "USD", open_shift_id: null, pumps: [], prices: [], my_stage });

beforeEach(async () => { await resetOffline(); localStorage.clear(); localStorage.setItem("fs_tutorial_done", "1"); signedIn(); });

describe("end of shift approval", () => {
  it("shows the attendant their end is waiting, with no start button, and lets them ask the owner to undo", async () => {
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: attendant, tenant } },
      "GET /api/shifts/prefill/": { body: { stations: [station({ stage: "held", end_id: "e1", ...end, can_fix: { cash: false, meters: false } })] } },
      "POST /api/shift-ends/e1/undo/": { status: 201, body: { id: "u1", status: "pending" } },
    });
    renderApp("/");
    const user = userEvent.setup();
    expect(await screen.findByRole("heading", { name: "Waiting for your manager" })).toBeInTheDocument();
    expect(screen.getByText(/cash difference/)).toHaveTextContent("-30.00");
    expect(screen.queryByRole("button", { name: "Start shift" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ask to undo" }));
    await user.type(screen.getByLabelText("Why"), "Found 30 more in the drawer");
    await user.click(screen.getByRole("button", { name: "Send to the owner" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/shift-ends/e1/undo/")?.body).toEqual({ kind: "cash", reason: "Found 30 more in the drawer" }));
  });

  it("lets the attendant fix an end the manager sent back", async () => {
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: attendant, tenant } },
      "GET /api/shifts/prefill/": { body: { stations: [station({ stage: "held", end_id: "e1", ...end, status: "sent_back", note: "Recount the cash", can_fix: { cash: true, meters: true } })] } },
      "POST /api/shift-ends/e1/fix/": { body: {} },
    });
    renderApp("/");
    const user = userEvent.setup();
    expect(await screen.findByText("Manager's note: Recount the cash")).toBeInTheDocument();
    const cash = screen.getByLabelText("Cash (USD)");
    await user.clear(cash); await user.type(cash, "150");
    await user.click(screen.getByRole("button", { name: "Send the fix to my manager" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/shift-ends/e1/fix/")?.body).toEqual({
      cash: [{ payment_type: "cash", currency: "USD", amount: "150" }], readings: [{ nozzle: "n1", reading: "1100.00" }],
    }));
  });

  it("lets a manager approve an end, or send it back with a note", async () => {
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant } },
      "GET /api/shift-ends/": { body: [{ id: "e1", shift_started_at: "2026-10-20T04:00:00Z", ...end }, { id: "e2", shift_started_at: "2026-10-20T04:00:00Z", ...end, person: "Rudo" }] },
      "GET /api/approvals/summary/": { body: { ends: 2, undos: 0 } },
      "POST /api/shift-ends/e1/approve/": { body: {} },
      "POST /api/shift-ends/e2/send-back/": { body: {} },
    });
    renderApp("/approvals");
    const user = userEvent.setup();
    expect(await screen.findByText("Tendai · Main")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Undo requests" })).not.toBeInTheDocument(); // the owner's
    await user.click(screen.getAllByRole("button", { name: "Approve" })[0]);
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/shift-ends/e1/approve/")).toBe(true));
    await user.click(screen.getAllByRole("button", { name: "Send back to fix" })[1]);
    await user.type(screen.getByLabelText("What needs fixing?"), "Recount");
    await user.click(screen.getByRole("button", { name: "Send back" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/shift-ends/e2/send-back/")?.body).toEqual({ note: "Recount" }));
  });
});
