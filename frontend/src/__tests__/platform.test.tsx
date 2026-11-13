import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mockApi, platformUser, renderApp, signedIn } from "./helpers";

const MODULES = [
  { key: "pump_allocation", label: "Pump allocation", help: "Who works which pump.", kind: "choice", needs: [], default: "shared",
    choices: [{ value: "shared", label: "Shared pumps, one attendant per shift" }, { value: "shared_multi", label: "Shared pumps, several attendants" },
      { value: "assigned", label: "Assigned pumps, the manager gives them out" }, { value: "pick", label: "Assigned pumps, attendants pick theirs when they start" }] },
  { key: "expenses", label: "Expenses", help: "Record what was paid out.", kind: "on_off", choices: [], needs: [], default: false },
  { key: "dip_at_shift_end", label: "Attendants dip at the end of a shift", help: "The outgoing attendant dips.", kind: "on_off", choices: [], needs: [], default: false },
  { key: "attendants_see_dip_litres", label: "Attendants see the dip litres", help: "Litres from the chart.", kind: "on_off", choices: [], needs: ["dip_at_shift_end"], default: false },
  { key: "day_close", label: "Day close", help: "When the trading day ends.", kind: "choice", needs: [], default: "manager",
    choices: [{ value: "manager", label: "The manager closes it on the Dip screen, from 21:00" }, { value: "07:00", label: "At the 07:00 handover" }, { value: "18:00", label: "At the 18:00 handover" }] },
];
const MARK_1 = { pump_allocation: "assigned", expenses: false, dip_at_shift_end: false, attendants_see_dip_litres: false, day_close: "manager" };
const MARK_2 = { pump_allocation: "pick", expenses: true, dip_at_shift_end: true, attendants_see_dip_litres: true, day_close: "18:00" };
const MARKS = [
  { id: "m1", name: "Mark 1", description: "The Fuel Zone way.", modules: MARK_1, based_on: null, tenants: [{ id: "t9", name: "Msasa Fuel Services" }] },
  { id: "m2", name: "Mark 2", description: "The PESNAT way.", modules: MARK_2, based_on: null, tenants: [] },
];
const me = {
  "GET /api/auth/me/": { body: { user: platformUser, tenant: null } },
  "GET /api/platform/modules/": { body: { modules: MODULES } },
  "GET /api/platform/marks/": { body: { marks: MARKS } },
};
const summary = {
  id: "t9", name: "Msasa Fuel Services", slug: "msasa-fuel-services", status: "active", created_at: "2026-10-03T10:00:00Z",
  subscription: { status: "active", plan: "standard", paid_through: null }, station_count: 2,
  owner: { id: "o1", full_name: "Rudo Chikwanha", email: null, phone: "+263772123456" },
  stations: [{ id: "s1", name: "Msasa Road", address: "Msasa", status: "active" }, { id: "s2", name: "Borrowdale", address: "", status: "active" }],
};

describe("platform: businesses list", () => {
  it("shows each business with subscription status, stations and owner, and filters by status", async () => {
    signedIn();
    const { calls } = mockApi({
      ...me,
      "GET /api/platform/tenants/": { body: { count: 1, next: null, results: [summary] } },
    });
    renderApp("/platform");
    const card = (await screen.findByText("Msasa Fuel Services")).closest("a")!;
    expect(within(card).getByText("sub: active")).toBeInTheDocument();
    expect(within(card).getByText("2 stations")).toBeInTheDocument();
    expect(within(card).getByText("Rudo Chikwanha")).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "Suspended" }));
    await vi.waitFor(() => expect(calls.some((c) => c.path === "/api/platform/tenants/")).toBe(true));
    expect(screen.getByRole("button", { name: "Suspended" })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("platform: create business", () => {
  async function openForm() {
    signedIn();
    const api = mockApi({ ...me });
    renderApp("/platform/new");
    await screen.findByRole("heading", { name: "New business" });
    return api;
  }

  async function fill(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText("Business name"), "Msasa Fuel Services");
    await user.type(screen.getByLabelText("Owner name"), "Rudo Chikwanha");
    await user.type(screen.getByLabelText("Owner phone"), "0772123456");
    await user.type(screen.getAllByLabelText("Station name")[0], "Msasa Road");
    await user.click(screen.getByRole("button", { name: "+ Add another station" }));
    await user.type(screen.getAllByLabelText("Station name")[1], "Borrowdale");
    await user.click(await screen.findByLabelText(/^Mark 1/));
  }

  it("sends the right payload and shows the temporary password once, with copy", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    signedIn();
    const { calls } = mockApi({
      ...me,
      "POST /api/platform/tenants/": { status: 201, body: { ...summary, owner_credentials: { login: "+263772123456", temporary_password: "Kx7mQp3Rtw" } } },
    });
    renderApp("/platform/new");
    await screen.findByRole("heading", { name: "New business" });
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Create business" }));

    expect(await screen.findByTestId("temp-password")).toHaveTextContent("Kx7mQp3Rtw");
    expect(screen.getByText(/shown only once/i)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/platform/tenants/")?.body).toEqual({
      business_name: "Msasa Fuel Services", owner_name: "Rudo Chikwanha", owner_phone: "0772123456", owner_email: null,
      plan: "", paid_through: null, stations: [{ name: "Msasa Road", address: "" }, { name: "Borrowdale", address: "" }], mark: "m1", modules: MARK_1,
    });

    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("Login: +263772123456\nTemporary password: Kx7mQp3Rtw");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open business" })).toHaveAttribute("href", "/platform/tenants/t9");
  });

  it("starts from the Mark's modules and plugs in one from the other Mark", async () => {
    const user = userEvent.setup();
    signedIn();
    const { calls } = mockApi({
      ...me,
      "POST /api/platform/tenants/": { status: 201, body: { ...summary, owner_credentials: { login: "+263772123456", temporary_password: "Kx7mQp3Rtw" } } },
    });
    renderApp("/platform/new");
    await screen.findByRole("heading", { name: "New business" });
    await fill(user);
    expect(screen.getByRole("heading", { name: "Also available" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Attendants see the dip litres" })).not.toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "Expenses" })); // Mark 2's, plugged into Mark 1
    expect(screen.getByText("Mark 1: off")).toBeInTheDocument(); // shown as a change from the Mark
    await user.click(screen.getByRole("checkbox", { name: "Attendants see the dip litres" }));
    expect(screen.getByRole("checkbox", { name: "Attendants dip at the end of a shift" })).toBeChecked(); // what it needs comes with it
    await user.click(screen.getByRole("button", { name: "Create business" }));
    await screen.findByTestId("temp-password");
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/platform/tenants/")?.body.modules).toEqual({
      ...MARK_1, expenses: true, dip_at_shift_end: true, attendants_see_dip_litres: true,
    });
  });

  it("keeps what was typed and explains a duplicate owner", async () => {
    const user = userEvent.setup();
    signedIn();
    mockApi({
      ...me,
      "POST /api/platform/tenants/": { status: 400, body: { detail: "That email or phone number is already in use." } },
    });
    renderApp("/platform/new");
    await screen.findByRole("heading", { name: "New business" });
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Create business" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already in use");
    expect(screen.getByLabelText("Business name")).toHaveValue("Msasa Fuel Services");
    expect(screen.queryByTestId("temp-password")).not.toBeInTheDocument();
  });

  it("lets staff remove an extra station row and never sends blank stations", async () => {
    const user = userEvent.setup();
    const { calls } = await openForm();
    await user.type(screen.getByLabelText("Business name"), "Solo Fuel");
    await user.type(screen.getByLabelText("Owner name"), "Chipo");
    await user.type(screen.getByLabelText("Owner email"), "chipo@example.com");
    await user.click(screen.getByRole("button", { name: "+ Add another station" }));
    expect(screen.getAllByLabelText("Station name")).toHaveLength(2);
    await user.click(screen.getAllByRole("button", { name: "Remove station" })[1]);
    expect(screen.getAllByLabelText("Station name")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Create business" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a Mark"); // the way the business works is asked up front
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await user.click(screen.getByLabelText(/^Mark 2/));
    await user.click(screen.getByRole("button", { name: "Create business" }));
    await vi.waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body.stations).toEqual([]);
  });
});

describe("platform: business detail", () => {
  it("suspends only after confirmation, and shows a reset password once", async () => {
    const user = userEvent.setup();
    signedIn();
    let status = "active";
    const { calls } = mockApi({
      ...me,
      "GET /api/platform/tenants/t9/": () => ({ body: { ...summary, status } }),
      "PATCH /api/platform/tenants/t9/": (body) => { status = body.status; return { body: { ...summary, status } }; },
      "POST /api/platform/tenants/t9/reset-owner-password/": { body: { owner_credentials: { login: "+263772123456", temporary_password: "NewTemp4567" } } },
    });
    renderApp("/platform/tenants/t9");
    await screen.findByRole("heading", { name: "Msasa Fuel Services" });

    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await user.click(screen.getByRole("button", { name: "Suspend" }));
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);

    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await user.click(screen.getByRole("button", { name: "Suspend" }));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ status: "suspended" });
    expect(await screen.findByRole("button", { name: "Reactivate" })).toBeInTheDocument();

    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await user.click(screen.getByRole("button", { name: "Reset owner password" }));
    expect(await screen.findByTestId("temp-password")).toHaveTextContent("NewTemp4567");
  });

  it("adds a tank to a station and chooses which tank a nozzle draws from", async () => {
    const user = userEvent.setup();
    signedIn();
    const fuels = [{ id: "f1", name: "Petrol", code: "PETROL" }, { id: "f2", name: "Diesel", code: "DIESEL" }];
    const tank1 = { id: "t1", name: "Tank 1", fuel_code: "PETROL", capacity_litres: "20000.000", reserve_litres: "500.000", has_calibration: true };
    const tank2 = { id: "t2", name: "Tank 2", fuel_code: "DIESEL", capacity_litres: "15000.000", reserve_litres: "0.000", has_calibration: false };
    const nozzles = [{ id: "n1", pump: "Pump 1", label: "Nozzle 1", tank: "t1" }];
    const { calls } = mockApi({
      ...me,
      "GET /api/platform/tenants/t9/": { body: summary },
      "GET /api/platform/tenants/t9/stations/s1/tanks/": { body: { tanks: [tank1], nozzles, fuels } },
      "POST /api/platform/tenants/t9/stations/s1/tanks/": { status: 201, body: { tanks: [tank1, tank2], nozzles, fuels } },
      "PATCH /api/platform/tenants/t9/nozzles/n1/tank/": { body: { tanks: [tank1, tank2], nozzles: [{ ...nozzles[0], tank: "t2" }], fuels } },
    });
    renderApp("/platform/tenants/t9");
    await screen.findByRole("heading", { name: "Msasa Fuel Services" });
    await user.click(screen.getAllByText("Tanks")[0]);
    const form = await screen.findByRole("form", { name: "Add a tank at Msasa Road" });
    await user.type(within(form).getByLabelText("Tank name"), "Tank 2");
    await user.selectOptions(within(form).getByLabelText("Fuel"), "f2");
    await user.type(within(form).getByLabelText("Capacity (litres)"), "15000");
    await user.click(within(form).getByRole("button", { name: "+ Add tank" }));
    expect(await screen.findByText("Tank 2 added to Msasa Road.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST" && c.path.endsWith("/tanks/"))!.body).toEqual({ name: "Tank 2", fuel_type: "f2", capacity_litres: "15000", reserve_litres: "0" });
    expect(screen.getAllByRole("listitem").some((li) => /Tank 2 · DIESEL · 15.000 L .*no dip chart yet/.test(li.textContent ?? ""))).toBe(true);
    await user.selectOptions(screen.getByLabelText("Msasa Road Pump 1 Nozzle 1 tank"), "t2");
    expect(calls.find((c) => c.method === "PATCH" && c.path.endsWith("/nozzles/n1/tank/"))!.body).toEqual({ tank: "t2" });
  });

  it("ticks a business's modules and saves only what changed, after confirmation", async () => {
    const user = userEvent.setup();
    signedIn();
    const onMark1 = { ...summary, mark: { id: "m1", name: "Mark 1" }, modules: MARK_1, mark_differences: {} };
    const { calls } = mockApi({
      ...me,
      "GET /api/platform/tenants/t9/": { body: onMark1 },
      "PATCH /api/platform/tenants/t9/modules/": { body: onMark1 },
      "PATCH /api/platform/tenants/t9/mark/": { body: onMark1 },
    });
    renderApp("/platform/tenants/t9");
    const box = await screen.findByRole("checkbox", { name: "Expenses" });
    expect(box).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Save modules" })).toBeDisabled();
    await user.click(box);
    await user.selectOptions(screen.getByLabelText("Day close"), "18:00");
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await user.click(screen.getByRole("button", { name: "Save modules" }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Expenses, Day close"));
    await vi.waitFor(() => expect(calls.find((c) => c.method === "PATCH" && c.path.endsWith("/modules/"))?.body).toEqual({ modules: { expenses: true, day_close: "18:00" } }));

    await user.selectOptions(screen.getByLabelText("Mark"), "m2");
    await vi.waitFor(() => expect(calls.find((c) => c.path.endsWith("/mark/"))?.body).toEqual({ mark: "m2" }));
  });

  it("saves subscription changes and adds a station", async () => {
    const user = userEvent.setup();
    signedIn();
    const { calls } = mockApi({
      ...me,
      "GET /api/platform/tenants/t9/": { body: summary },
      "PATCH /api/platform/tenants/t9/subscription/": { body: {} },
      "POST /api/platform/tenants/t9/stations/": { status: 201, body: {} },
    });
    renderApp("/platform/tenants/t9");
    await screen.findByRole("heading", { name: "Msasa Fuel Services" });
    await user.selectOptions(screen.getByLabelText("Status"), "past_due");
    await user.click(screen.getByRole("button", { name: "Save subscription" }));
    expect(calls.find((c) => c.path.endsWith("/subscription/"))?.body).toEqual({ status: "past_due", plan: "standard", paid_through: null });

    await user.type(screen.getAllByLabelText("Station name")[0], "Chitungwiza");
    await user.click(screen.getByRole("button", { name: "+ Add station" }));
    expect(calls.find((c) => c.path.endsWith("/stations/"))?.body).toEqual({ name: "Chitungwiza", address: "" });
  });
});

describe("platform: Marks", () => {
  it("lists each Mark with its businesses, duplicates one, and saves only the changed modules", async () => {
    const user = userEvent.setup();
    signedIn();
    const { calls } = mockApi({
      ...me,
      "POST /api/platform/marks/": { status: 201, body: { ...MARKS[1], id: "m3", name: "Mark 3" } },
      "PATCH /api/platform/marks/m2/": { body: MARKS[1] },
    });
    renderApp("/platform/marks");
    const mark1 = await screen.findByRole("region", { name: "Mark 1" });
    expect(within(mark1).getByText("Businesses on it: Msasa Fuel Services.")).toBeInTheDocument();
    const mark2 = screen.getByRole("region", { name: "Mark 2" });
    await user.type(within(mark2).getByLabelText("New Mark from Mark 2"), "Mark 3");
    await user.click(within(mark2).getByRole("button", { name: "Duplicate" }));
    await vi.waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ duplicate_of: "m2", name: "Mark 3" }));

    await user.click(within(mark2).getByText("Change Mark 2"));
    await user.click(within(mark2).getByRole("checkbox", { name: "Expenses" }));
    await user.click(within(mark2).getByRole("button", { name: "Save Mark 2" }));
    await vi.waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ name: "Mark 2", description: "The PESNAT way.", modules: { expenses: false } }));
  });
});
