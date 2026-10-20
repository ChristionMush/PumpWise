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
    });

    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("Login: +263772123456\nTemporary password: Kx7mQp3Rtw");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open business" })).toHaveAttribute("href", "/platform/tenants/t9");
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
