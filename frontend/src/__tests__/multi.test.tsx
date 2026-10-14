import "fake-indexeddb/auto";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { nextBatch, resetOffline, type QueuedOp } from "../offline";
import { mockApi, renderApp, signedIn } from "./helpers";

const attendant = { user: { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" }, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } };
const nozzle = (id: string, fuel = "p", code = "PETROL") => ({ id, number: id.endsWith("2") ? 2 : 1, label: code === "PETROL" ? "Petrol" : "Diesel", meter_max: "99999999.99", last_reading: "1000.00", fuel_type: fuel, fuel_code: code });
const pump = { id: "pu1", name: "Pump 1", nozzles: [nozzle("n1")] };
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

});

describe("assigned pumps", () => {
  const myPump = { id: "pu1", name: "Pump 1", nozzles: [nozzle("n1"), nozzle("n2", "d", "DIESEL")], assigned_to: "a", assigned_to_name: "Tendai" };
  const assigned = { ...base, attendants_mode: "multiple", assignment_mode: "assigned", pumps: [myPump] };

  it("shows an attendant only their own pumps, with no way to assign or swap them", async () => {
    const { calls } = mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [assigned] } }, "POST /api/sync/": applyAll });
    renderApp("/");
    const user = userEvent.setup();
    expect(await screen.findByText("Your pumps: Pump 1")).toBeInTheDocument();
    expect(screen.queryByText(/Who works/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Hand a pump/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Pumps" })).not.toBeInTheDocument();
    const meters = screen.getAllByPlaceholderText("Meter number");
    expect(meters).toHaveLength(2); // both nozzles of my pump, one per tank
    await user.clear(meters[1]);
    await user.click(screen.getByRole("button", { name: "Start shift" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("both nozzles");
    await user.type(meters[1], "500");
    await user.click(screen.getByRole("button", { name: "Start shift" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/sync/")).toBe(true));
    const start = calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops)[0];
    expect(start.kind).toBe("shift_start");
    expect(start.payload.assignments).toBeUndefined();
    expect(start.payload.readings.map((r: any) => r.nozzle)).toEqual(["n1", "n2"]);
  });

  it("tells an attendant with no pumps to ask their manager", async () => {
    mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [{ ...assigned, pumps: [] }] } } });
    renderApp("/");
    expect(await screen.findByText(/No pump is assigned to you/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start shift" })).not.toBeInTheDocument();
  });

  it("lets a manager swap a pump in use, with the meter numbers taken now", async () => {
    const manager = { ...attendant, user: { ...attendant.user, id: "m", role: "manager", full_name: "Manager" } };
    const station = { ...assigned, staff: [{ id: "a", name: "Tendai" }, { id: "b", name: "Rudo" }], pumps: [{ ...myPump, in_use_by: "Tendai" }] };
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: manager }, "GET /api/shifts/prefill/": { body: { stations: [station] } },
      "POST /api/pumps/pu1/assign/": { body: { id: "pu1" } },
    });
    renderApp("/pumps");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Who works Pump 1"), "b");
    await user.click(screen.getByRole("button", { name: "Swap pump" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("meter number of both nozzles");
    await user.type(screen.getByLabelText(/Petrol \(PETROL\): meter number now/), "1500");
    await user.type(screen.getByLabelText(/Diesel \(DIESEL\): meter number now/), "800");
    await user.click(screen.getByRole("button", { name: "Swap pump" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/pumps/pu1/assign/")).toBe(true));
    const body = calls.find((c) => c.path === "/api/pumps/pu1/assign/")!.body;
    expect(body.user).toBe("b");
    expect(body.readings).toEqual([{ nozzle: "n1", reading: "1500" }, { nozzle: "n2", reading: "800" }]);
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
