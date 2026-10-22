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

describe("day close", () => {
  it("needs every tank's dip and every pump's meter, then closes the day in one entry", async () => {
    signedIn();
    const tank = { id: "k1", name: "Petrol Tank", fuel_code: "PETROL", capacity_litres: "15030", has_calibration: false, last_litres: "5000" };
    const pump = { id: "p1", name: "4A", nozzles: [{ id: "n1", number: 1, label: "Petrol", meter_max: "99999999.99", last_reading: "924759.36", fuel_type: "f1", fuel_code: "PETROL" }] };
    const station = { id: "s1", name: "Chigwende", pricing_currency: "USD", open_shift_id: null, pumps: [pump], prices: [], tanks: [tank] };
    const status = [{ station: "s1", day: "2026-10-22", closed: null, recent: [] }];
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant } }, "GET /api/shifts/prefill/": { body: { stations: [station] } },
      "GET /api/day-close/": { body: status }, "POST /api/day-close/": { status: 201, body: status[0] },
    });
    renderApp("/dip");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Close the day" });
    await user.type(within(form).getByLabelText("Litres"), "5400");
    await user.click(within(form).getByRole("button", { name: "Close the day" }));
    expect(await screen.findByText(/Missing: 4A Petrol/)).toBeInTheDocument();
    await user.type(within(form).getByLabelText(/4A · Petrol/), "925100.5");
    await user.click(within(form).getByRole("button", { name: "Close the day" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.path === "/api/day-close/")?.body).toEqual({
      station: "s1", note: "", dips: [{ tank: "k1", litres: "5400" }], meters: [{ nozzle: "n1", reading: "925100.5" }],
    }));
  });

  it("closes an earlier day afterwards, at the time it was done, so its figures are recalculated", async () => {
    signedIn();
    const tank = { id: "k1", name: "Petrol Tank", fuel_code: "PETROL", capacity_litres: "15030", has_calibration: true, last_litres: "5000" };
    const pump = { id: "p1", name: "4A", nozzles: [{ id: "n1", number: 1, label: "Petrol", meter_max: "99999999.99", last_reading: "924759.36", fuel_type: "f1", fuel_code: "PETROL" }] };
    const station = { id: "s1", name: "Chigwende", pricing_currency: "USD", open_shift_id: null, pumps: [pump], prices: [], tanks: [tank] };
    const closed = { day: "2026-10-22", closed_at: "2026-10-22T19:05:00Z", closed_by: "Justice" };
    const status = [{ station: "s1", day: "2026-10-22", closed, recent: [{ id: "c1", ...closed }] }];
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant } }, "GET /api/shifts/prefill/": { body: { stations: [station] } },
      "GET /api/day-close/": { body: status }, "POST /api/day-close/": { status: 201, body: status[0] },
    });
    renderApp("/dip");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Close the day" });
    expect(within(form).queryByLabelText(/4A · Petrol/)).not.toBeInTheDocument(); // today is closed
    await user.click(within(form).getByRole("button", { name: "Close an earlier day" }));
    const day = within(form).getByLabelText("Day to close");
    await user.clear(day); await user.type(day, "2026-10-20");
    const time = within(form).getByLabelText("Time of the close");
    await user.clear(time); await user.type(time, "21:15");
    expect(within(form).getByLabelText(/4A · Petrol/)).not.toHaveTextContent(/last 924759/); // today's number would mislead
    await user.type(within(form).getByLabelText("Dip (mm)"), "1290");
    await user.type(within(form).getByLabelText(/4A · Petrol/), "924000");
    await user.click(within(form).getByRole("button", { name: /^Close Tue/ }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.path === "/api/day-close/")?.body).toEqual({
      station: "s1", note: "", at: new Date("2026-10-20T21:15").toISOString(), dips: [{ tank: "k1", dip_mm: "1290" }], meters: [{ nozzle: "n1", reading: "924000" }],
    }));
    expect(await screen.findByText(/Tue, 20 Oct is closed\. Its sales, fuel check and report are recalculated\.|20 Oct.* is closed/)).toBeInTheDocument();
  });
});

describe("daily report on the Today dashboard", () => {
  it("shows the last closed day per station, with intercompany owed and the pumps and shifts as the deep dive", async () => {
    signedIn();
    const DASH = { as_of: "2026-10-22T19:30:00Z", totals: { today_litres: "0", today_amount_by_currency: {}, open_alerts: 0, unexplained_loss_7d: "0", flagged_cash_shifts_7d: 0, open_shifts: 0 }, next_refill: null, stations: [] };
    const report = {
      station: "s1", station_name: "Chigwende", day: "2026-10-22", currency: "USD", closed: true, closed_at: "2026-10-22T19:12:00Z", closed_by: "Justice", so_far: false,
      totals: { litres: "1659.000", amount: "3393.88", handed_in: "3390.92", grand_total: "3390.92", received: "3190.92", intercompany: "200.00" },
      cash_losses: { total: "3.46", attendants: "2.50", by_attendant: [{ attendant: "Nancy Moyo", amount: "2.50" }], milking: "0.96", milking_count: 3 },
      dips: [{ fuel: "Petrol (Unleaded)", fuel_code: "PETROL", litres: "5240.000", tanks: [{ tank: "Petrol Tank", dip_mm: "1312.0", litres: "5240.000", at: "2026-10-22T19:12:00Z" }] }],
      fuels: [{ fuel: "Diesel", fuel_code: "DIESEL", litres: "707.000", amount: "1442.28" }, { fuel: "Petrol (Unleaded)", fuel_code: "PETROL", litres: "952.000", amount: "1951.60" }],
      pumps: [{ pump: "4A", nozzle: "Petrol", opening_meter: "924759.36", closing_meter: "925100.36", meter_litres: "341.000", sold_litres: "341.000" }],
      tanks: [{ tank: "Petrol Tank", fuel_code: "PETROL", opening: "6199", deliveries: "0", dispensed: "952", closing: "5240", loss: "7.000", status: "ok" }],
      handed_in: [{ payment_type: "cash", label: "Cash", amount: "3190.92" }, { payment_type: "card", label: "Swipe", amount: "0.00" }, { payment_type: "mobile_money", label: "EcoCash", amount: "0.00" }, { payment_type: "credit", label: "Inter Company Credit", amount: "200.00" }],
      people: [{ attendant: "Latifah Taibu", shift_id: "sh1", shift_started_at: "2026-10-22T11:10:00Z", litres: "341.000", amount: "699.05" }],
    };
    mockApi({ "GET /api/auth/me/": { body: { user: ownerUser, tenant } }, "GET /api/dashboard/": { body: DASH }, "GET /api/prices/announcements/": { body: { announcements: [] } }, "GET /api/reports/daily/": { body: [report] } });
    renderApp("/");
    const card = await screen.findByRole("region", { name: "Daily report Chigwende" });
    expect(card).toHaveTextContent(/closed .* by Justice/);
    expect(card).toHaveTextContent(/Date: Thursday,? 22 October 2026|Date: Thursday,? October 22,? 2026/);
    const total = within(card).getByRole("table", { name: "Grand total" });
    expect(total).toHaveTextContent(/Grand Total\s*USD 3\D?390\D92/);
    expect(total).toHaveTextContent(/Cash\s*USD 3\D?190\D92.*Swipe\s*USD 0\D00.*EcoCash\s*USD 0\D00.*Inter Company Credit \(owed by the other company\)\s*USD 200\D00/);
    const losses = within(card).getByRole("table", { name: "Cash losses" });
    expect(losses).toHaveTextContent(/Cash Losses Total\s*USD 3\D46.*Nancy Moyo\s*USD 2\D50.*Milking errors \(under \$1, 3\)\s*USD 0\D96/);
    expect(within(card).getByRole("table", { name: "Dips" })).toHaveTextContent(/Petrol \(Unleaded\)\s*5\D?240 L.*Petrol Tank\s*1\D?312 mm\s*5\D?240 L/);
    expect(card).toHaveTextContent(/Latifah Taibu/); // the deep dive
  });
});
