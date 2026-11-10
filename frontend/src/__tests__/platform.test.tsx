import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mockApi, platformUser, renderApp, signedIn } from "./helpers";

const me = { "GET /api/auth/me/": { body: { user: platformUser, tenant: null } } };
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
    await user.click(screen.getByLabelText(/Assigned pumps/));
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
      plan: "", paid_through: null, stations: [{ name: "Msasa Road", address: "" }, { name: "Borrowdale", address: "" }], pump_system: "assigned",
      expenses_enabled: false, attendants_pick_pumps: false, dip_at_shift_end: false, close_day_at_change: "",
    });

    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("Login: +263772123456\nTemporary password: Kx7mQp3Rtw");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open business" })).toHaveAttribute("href", "/platform/tenants/t9");
  });

  it("turns on expenses for a new business when ticked", async () => {
    const user = userEvent.setup();
    signedIn();
    const { calls } = mockApi({
      ...me,
      "POST /api/platform/tenants/": { status: 201, body: { ...summary, owner_credentials: { login: "+263772123456", temporary_password: "Kx7mQp3Rtw" } } },
    });
    renderApp("/platform/new");
    await screen.findByRole("heading", { name: "New business" });
    await fill(user);
    await user.click(screen.getByLabelText("Expenses"));
    await user.click(screen.getByRole("button", { name: "Create business" }));
    await screen.findByTestId("temp-password");
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/platform/tenants/")?.body.expenses_enabled).toBe(true);
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
    expect(await screen.findByRole("alert")).toHaveTextContent("shared or assigned"); // the pump system is asked up front
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await user.click(screen.getByLabelText(/Shared pumps, one attendant/));
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

  it("switches expenses on a business's page", async () => {
    const user = userEvent.setup();
    signedIn();
    const features = { multi_attendant_enabled: true, pump_assignment_enabled: true, expenses_enabled: false };
    const { calls } = mockApi({
      ...me,
      "GET /api/platform/tenants/t9/": { body: { ...summary, features } },
      "PATCH /api/platform/tenants/t9/features/": { body: { ...summary, features: { ...features, expenses_enabled: true } } },
    });
    renderApp("/platform/tenants/t9");
    const box = await screen.findByLabelText("Expenses");
    expect(box).not.toBeChecked();
    await user.click(box);
    expect(calls.find((c) => c.path.endsWith("/features/"))?.body).toEqual({ expenses_enabled: true });
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
