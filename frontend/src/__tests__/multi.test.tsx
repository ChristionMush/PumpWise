import "fake-indexeddb/auto";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { nextBatch, resetOffline, type QueuedOp } from "../offline";
import { mockApi, renderApp, signedIn } from "./helpers";

const attendant = { user: { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" }, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } };
const pump = { id: "pu1", name: "Pump 1", qr_token: "qr1", fuel_type: "p", fuel_code: "PETROL", nozzles: [{ id: "n1", number: 1, label: "Nozzle 1", meter_max: "99999999.99", last_reading: "1000.00" }] };
const base = { id: "st1", name: "Main", pricing_currency: "USD", open_shift_id: null, pumps: [pump], prices: [{ fuel_type: "p", currency: "USD", price: "2.00", from: "2026-01-01T00:00:00Z", to: null }] };
const applyAll = (body: any) => ({ body: { results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "applied" })) } });

beforeEach(async () => { await resetOffline(); localStorage.clear(); localStorage.setItem("fs_tutorial_done", "1"); signedIn(); });

describe("attendants and the tank side (rule 11)", () => {
  it("has no dip or delivery screen for an attendant, even by typing the address", async () => {
    mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [base] } } });
    renderApp("/dip");
    expect(await screen.findByRole("heading", { name: "Start shift" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Dip & delivery" })).not.toBeInTheDocument();
  });
});

describe("several attendants on one shift", () => {
  it("joins the open shift with one tap and hands in only their own cash", async () => {
    const station = { ...base, attendants_mode: "multiple", joinable_shift: { id: "sh1", started_at: "2026-10-14T06:00:00Z", people: ["Rudo"] } };
    const { calls } = mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [station] } }, "POST /api/sync/": applyAll });
    renderApp("/");
    const user = userEvent.setup();
    expect(await screen.findByText(/already open here \(Rudo\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Join this shift" }));
    expect(await screen.findByRole("heading", { name: "Hand in my cash" })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Meter number now")).not.toBeInTheDocument(); // the lead enters meters, once
    await user.type(screen.getByLabelText("Cash"), "50");
    await user.click(screen.getByRole("button", { name: "Hand in my cash" }));
    expect(await screen.findByText(/The lead closes the shift/)).toBeInTheDocument();
    await waitFor(() => expect(calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops).length).toBe(2));
    const ops = calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops);
    expect(ops.map((o: any) => [o.kind, o.payload.shift_id])).toEqual([["shift_join", "sh1"], ["shift_cash", "sh1"]]);
    expect(ops[1].payload.cash).toEqual([{ payment_type: "cash", currency: "USD", amount: "50" }]);
  });

  it("pre-fills pump assignments from the last shift so starting is one tap", async () => {
    const station = { ...base, attendants_mode: "multiple", assignment_mode: "assigned", staff: [{ id: "a", name: "Tendai" }, { id: "b", name: "Rudo" }], previous_assignments: { pu1: "b" } };
    const { calls } = mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [station] } }, "POST /api/sync/": applyAll });
    renderApp("/");
    const user = userEvent.setup();
    expect(await screen.findByRole("heading", { name: "Who works each pump" })).toBeInTheDocument();
    expect(screen.getByLabelText("Who works Pump 1")).toHaveValue("b");
    await user.click(screen.getByRole("button", { name: "Start shift" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/sync/")).toBe(true));
    const start = calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops)[0];
    expect(start.payload.assignments).toEqual({ pu1: "b" });
    expect(start.payload.attendants).toEqual(["b"]);
  });
});

describe("sync batches", () => {
  it("sends one batch per shift, in queue order", () => {
    const op = (id: string, shift?: string): QueuedOp => ({ op_id: id, kind: "shift_end", payload: shift ? { shift_id: shift } : {}, queued_at: 0 });
    expect(nextBatch([op("1", "s1"), op("2", "s1"), op("3", "s2"), op("4", "s1")]).map((o) => o.op_id)).toEqual(["1", "2"]);
    expect(nextBatch([op("1"), op("2"), op("3", "s1")]).map((o) => o.op_id)).toEqual(["1", "2"]);
    expect(nextBatch([])).toEqual([]);
  });
});

describe("demo businesses", () => {
  it("shows a visible Demo data banner", async () => {
    mockApi({
      "GET /api/auth/me/": { body: { ...attendant, tenant: { ...attendant.tenant, is_demo: true, trial_expires_at: "2026-10-28" } } },
      "GET /api/shifts/prefill/": { body: { stations: [base] } },
    });
    renderApp("/");
    expect(await screen.findByText(/Demo data · trial until 2026-10-28/)).toBeInTheDocument();
  });
});
