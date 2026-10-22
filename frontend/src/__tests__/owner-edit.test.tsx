import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

const tenant = { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" };
const manager = { ...ownerUser, id: "m1", role: "manager", full_name: "Justice" };
const tendai = { id: "a1", role: "attendant", full_name: "Tendai", phone: "+263772000111", email: null, station_ids: [] };
const settings = (user: any) => ({
  "GET /api/auth/me/": { body: { user, tenant } },
  "GET /api/settings/": { body: { summary_phone: "", summary_channel: "sms" } },
  "GET /api/exports/": { body: [] },
  "GET /api/users/": { body: { results: [{ ...ownerUser, station_ids: [] }, tendai] } },
  "GET /api/stations/": { body: { results: [] } },
});

describe("owner edits the team", () => {
  it("changes a team member's details and sets a new password for them", async () => {
    signedIn();
    const { calls } = mockApi({ ...settings(ownerUser), "PATCH /api/users/a1/": { body: tendai } });
    renderApp("/settings");
    const user = userEvent.setup();
    const row = (await screen.findByText(/Tendai/)).closest("div")!;
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("group", { name: "Edit Tendai" });
    await user.clear(within(form).getByLabelText("Phone"));
    await user.type(within(form).getByLabelText("Phone"), "0772999888");
    await user.type(within(form).getByLabelText(/New password/), "Fresh-pass-123");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ full_name: "Tendai", phone: "0772999888", email: null, role: "attendant", password: "Fresh-pass-123" });
    expect(await screen.findByText(/choose their own password at their next sign-in/)).toBeInTheDocument();
  });

  it("lets the owner change their own password", async () => {
    signedIn();
    mockApi(settings(ownerUser));
    renderApp("/settings");
    expect(await screen.findByRole("link", { name: "Change my password" })).toHaveAttribute("href", "/change-password");
  });

  it("gives managers no edit buttons", async () => {
    signedIn();
    mockApi(settings(manager));
    renderApp("/settings");
    expect(await screen.findByText(/Tendai/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });
});

describe("owner edits tanks, pumps and nozzles", () => {
  const tank = { id: "k1", station: "s1", name: "Petrol Tank", fuel_type: "f1", fuel_code: "PETROL", capacity_litres: "15030", reserve_litres: "271", has_calibration: true };
  const other = { ...tank, id: "k2", name: "Diesel Tank", fuel_code: "DIESEL" };
  const pump = { id: "p1", station: "s1", name: "4A", brand: "", model: "", year: null, nozzles: [{ id: "n1", number: 1, label: "", tank: "k1", tank_name: "Petrol Tank", fuel_code: "PETROL" }] };
  const setup = (user: any) => ({
    "GET /api/auth/me/": { body: { user, tenant } },
    "GET /api/stations/": { body: { results: [{ id: "s1", name: "Chigwende" }] } },
    "GET /api/fuel-types/": { body: { results: [] } },
    "GET /api/tanks/": { body: { results: [tank, other] } },
    "GET /api/pumps/": { body: { results: [pump] } },
    "GET /api/onboarding/checklist/": { body: { items: [], done: 1, total: 1, percent: 100, ready: true } },
  });

  it("sends only what changed for a tank, and moves a nozzle to another tank", async () => {
    signedIn();
    const { calls } = mockApi({ ...setup(ownerUser), "PATCH /api/tanks/k1/": { body: tank }, "PATCH /api/nozzles/n1/": { body: {} } });
    renderApp("/setup");
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "Edit" }))[0]);
    const form = screen.getByRole("form", { name: "Edit Petrol Tank" });
    await user.clear(within(form).getByLabelText("Reserve level (litres)"));
    await user.type(within(form).getByLabelText("Reserve level (litres)"), "500");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/tanks/k1/")?.body).toEqual({ reserve_litres: "500" }));

    await waitFor(() => expect(screen.queryByRole("form", { name: "Edit Petrol Tank" })).not.toBeInTheDocument());
    const edits = screen.getAllByRole("button", { name: "Edit" }); // two tanks, then the pump
    await user.click(edits[edits.length - 1]); // the pump
    await user.click(screen.getByRole("button", { name: "Edit nozzle" }));
    const nozzle = screen.getByRole("form", { name: "Edit 4A, nozzle 1" });
    await user.selectOptions(within(nozzle).getByLabelText("Draws from"), "k2");
    await user.click(within(nozzle).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/nozzles/n1/")?.body).toEqual({ tank: "k2" }));
  });

  it("gives managers no edit buttons in Setup", async () => {
    signedIn();
    mockApi(setup(manager));
    renderApp("/setup");
    expect((await screen.findAllByText(/Petrol Tank/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });
});

describe("fuel delivery", () => {
  it("asks which fuel is arriving and sends it with the tank of that fuel", async () => {
    signedIn();
    const tank = (id: string, name: string, fuel_code: string) => ({ id, name, fuel_code, capacity_litres: "15030", has_calibration: true, last_litres: "5000" });
    const station = { id: "s1", name: "Chigwende", pricing_currency: "USD", open_shift_id: null, pumps: [], prices: [], tanks: [tank("k1", "Petrol Tank", "PETROL"), tank("k2", "Diesel Tank", "DIESEL")] };
    const applyAll = (body: any) => ({ body: { results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "applied" })) } });
    const { calls } = mockApi({ "GET /api/auth/me/": { body: { user: manager, tenant } }, "GET /api/shifts/prefill/": { body: { stations: [station] } }, "POST /api/sync/": applyAll });
    renderApp("/dip");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Fuel received"), "DIESEL");
    expect(screen.getByText(/Goes into Chigwende · Diesel Tank/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Litres received"), "10000");
    await user.click(screen.getByRole("button", { name: "Save delivery" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/sync/")).toBe(true));
    const op = calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops).find((o: any) => o.kind === "delivery");
    expect([op.payload.tank, op.payload.fuel_code, op.payload.litres]).toEqual(["k2", "DIESEL", "10000"]);
  });
});
