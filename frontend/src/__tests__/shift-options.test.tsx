import "fake-indexeddb/auto";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { kvSet, resetOffline } from "../offline";
import { mockApi, renderApp, signedIn } from "./helpers";

// A business where attendants pick their pumps and dip at the end of a shift (each switched on by the platform).
const attendant = { user: { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" }, tenant: { id: "t", name: "Pesnat", slug: "p", status: "active" } };
const nozzle = (id: string, tank: string) => ({ id, number: 1, label: "Petrol", meter_max: "99999999.99", last_reading: "1000.00", fuel_type: "p", fuel_code: "PETROL", tank });
const pumpA = { id: "pa", name: "Pump A", nozzles: [nozzle("na", "tk1")] };
const pumpB = { id: "pb", name: "Pump B", nozzles: [nozzle("nb", "tk2")], in_use_by: "Rudo" };
const station = {
  id: "st1", name: "Mverechena", pricing_currency: "USD", open_shift_id: null, attendants_mode: "multiple", assignment_mode: "assigned",
  prices: [{ fuel_type: "p", currency: "USD", price: "1.50", from: "2026-01-01T00:00:00Z", to: null }],
  pumps: [pumpA, pumpB], pick_pumps: true, dip_at_end: true, last_dip_at: null,
  tanks: [{ id: "tk1", name: "Tank 1", fuel_code: "PETROL", has_calibration: true, last_dip_at: null }, { id: "tk2", name: "Tank 2", fuel_code: "PETROL", has_calibration: true, last_dip_at: null }],
};
const applyAll = (body: any) => ({ body: { results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "applied" })) } });
const ops = (calls: any[]) => calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops);

beforeEach(async () => { await resetOffline(); localStorage.clear(); localStorage.setItem("fs_tutorial_done", "1"); signedIn(); });

describe("attendants pick their pumps", () => {
  it("ticks a free pump (one still with someone else cannot be ticked) and starts on it", async () => {
    const { calls } = mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [station] } }, "POST /api/sync/": applyAll });
    renderApp("/");
    const user = userEvent.setup();
    const a = await screen.findByRole("checkbox", { name: "Pump A" });
    expect(screen.getByRole("checkbox", { name: /Pump B/ })).toBeDisabled();
    expect(screen.getByText(/still with Rudo/)).toBeInTheDocument();
    expect(screen.queryByText(/Your pumps:/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start shift" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Tick the pumps you are taking");
    await user.click(a);
    await user.click(screen.getByRole("button", { name: "Start shift" }));
    await waitFor(() => expect(ops(calls).some((o: any) => o.kind === "shift_start")).toBe(true));
    const start = ops(calls).find((o: any) => o.kind === "shift_start");
    expect(start.payload.pumps).toEqual(["pa"]);
    expect(start.payload.readings.map((r: any) => r.nozzle)).toEqual(["na"]);
  });
});

describe("dip at the end of a shift", () => {
  const onShift = async () => kvSet("activeShift", { id: "s1", station_id: "st1", started_at: "2026-11-10T05:00:00Z", starts: { na: 1000 } });

  it("asks the outgoing attendant to dip only the tank of the pump they are on, and sends it with the end", async () => {
    await onShift();
    const { calls } = mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [station] } }, "POST /api/sync/": applyAll });
    renderApp("/");
    const user = userEvent.setup();
    await user.type(await screen.findByPlaceholderText("Meter number now"), "1100");
    await user.type(screen.getByLabelText("Cash"), "150");
    await user.click(screen.getByRole("button", { name: "End shift" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Dip Tank 1 before you end");
    expect(screen.getByRole("region", { name: "Tank dips" })).not.toHaveTextContent("Tank 2"); // Pump B's tank: not theirs
    await user.type(screen.getByLabelText("Dip (mm)"), "1200");
    await user.click(screen.getByRole("button", { name: "End shift" }));
    await waitFor(() => expect(ops(calls).some((o: any) => o.kind === "shift_end")).toBe(true));
    expect(ops(calls).find((o: any) => o.kind === "shift_end").payload.dips).toEqual([{ tank: "tk1", dip_mm: "1200" }]);
  });

  it("does not ask again when their tank was dipped in the last hour (the first to end dips)", async () => {
    await onShift();
    const justDipped = { ...station, tanks: station.tanks.map((t) => (t.id === "tk1" ? { ...t, last_dip_at: new Date(Date.now() - 5 * 60 * 1000).toISOString() } : t)) };
    const { calls } = mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [justDipped] } }, "POST /api/sync/": applyAll });
    renderApp("/");
    const user = userEvent.setup();
    await user.type(await screen.findByPlaceholderText("Meter number now"), "1100");
    expect(screen.queryByRole("region", { name: "Tank dips" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Cash"), "150");
    await user.click(screen.getByRole("button", { name: "End shift" }));
    await waitFor(() => expect(ops(calls).some((o: any) => o.kind === "shift_end")).toBe(true));
    expect(ops(calls).find((o: any) => o.kind === "shift_end").payload.dips).toBeUndefined();
  });
});

describe("a tank runs dry", () => {
  it("switches the nozzle to another tank of the same fuel at the meter number now, saved on the phone first", async () => {
    await kvSet("activeShift", { id: "s1", station_id: "st1", started_at: "2026-11-10T05:00:00Z", starts: { na: 1000 } });
    const withBackup = { ...station, dip_at_end: false, pumps: [{ ...pumpA, nozzles: [{ ...pumpA.nozzles[0], switch_to: [{ id: "tk3", name: "Tank 3" }] }] }] };
    const { calls } = mockApi({ "GET /api/auth/me/": { body: attendant }, "GET /api/shifts/prefill/": { body: { stations: [withBackup] } }, "POST /api/sync/": applyAll });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByText(/Tank ran dry\? Switch Pump A Petrol/));
    const group = screen.getByRole("group", { name: "Switch Pump A Petrol to another tank" });
    await user.click(within(group).getByRole("button", { name: "Switch tank" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("meter number at the switch");
    await user.type(within(group).getByLabelText("Meter number at the switch"), "1060");
    await user.click(within(group).getByRole("button", { name: "Switch tank" }));
    expect(await screen.findByText(/now draws from Tank 3/)).toBeInTheDocument();
    await waitFor(() => expect(ops(calls).some((o: any) => o.kind === "tank_switch")).toBe(true));
    expect(ops(calls).find((o: any) => o.kind === "tank_switch").payload).toMatchObject({ nozzle: "na", tank: "tk3", reading: "1060" });
  });
});
